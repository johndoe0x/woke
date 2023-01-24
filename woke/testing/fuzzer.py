import copy
import inspect
import json
import logging
import multiprocessing
import multiprocessing.connection
import multiprocessing.synchronize
import os
import pickle
import random
import subprocess
import sys
import time
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from time import sleep
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.error import URLError

import rich.progress
from pathvalidate import sanitize_filename  # type: ignore
from rich.traceback import Traceback
from tblib import pickling_support

from woke.cli.console import console
from woke.config import WokeConfig
from woke.testing.core import default_chain
from woke.testing.coverage import (
    Coverage,
    CoverageProvider,
    IdeFunctionCoverageRecord,
    IdePosition,
    export_merged_ide_coverage,
)
from woke.testing.globals import attach_debugger, set_exception_handler
from woke.utils.tee import StderrTee, StdoutTee


def _setup(port: int, network_id: str) -> subprocess.Popen:
    if network_id == "anvil":
        args = [
            "anvil",
            "--port",
            str(port),
            "--prune-history",
            "--gas-price",
            "0",
            "--base-fee",
            "0",
            "--steps-tracing",
        ]
    elif network_id == "ganache":
        args = ["ganache-cli", "--port", str(port), "-g", "0", "-k", "istanbul"]
    elif network_id == "hardhat":
        args = ["npx", "hardhat", "node", "--port", str(port)]
    else:
        raise ValueError(f"Unknown network ID '{network_id}'")

    return subprocess.Popen(args, stdout=subprocess.DEVNULL)


def _run_core(
    fuzz_test: Callable,
    index: int,
    random_seed: bytes,
    finished_event: multiprocessing.synchronize.Event,
    err_child_conn: multiprocessing.connection.Connection,
    cov_child_conn: multiprocessing.connection.Connection,
    coverage: Optional[Coverage],
):
    print(f"Using seed '{random_seed.hex()}' for process #{index}")

    try:
        default_chain.reset()
    except NotImplementedError:
        logging.warning("Development chain does not support resetting")

    args = []
    for arg in inspect.getfullargspec(fuzz_test).args:
        if arg == "coverage":
            if coverage is not None:
                args.append(
                    (
                        CoverageProvider(
                            coverage, default_chain.chain_interface.get_block_number()
                        ),
                        cov_child_conn,
                    )
                )
            else:
                args.append(None)
        else:
            raise ValueError(
                f"Unable to set value for '{arg}' argument in '{fuzz_test.__name__}' function."
            )

    fuzz_test(*args)

    err_child_conn.send(None)
    finished_event.set()


def _run(
    fuzz_test: Callable,
    index: int,
    port: int,
    random_seed: bytes,
    log_file: Path,
    tee: bool,
    finished_event: multiprocessing.synchronize.Event,
    err_child_conn: multiprocessing.connection.Connection,
    cov_child_conn: multiprocessing.connection.Connection,
    network_id: str,
    coverage: Optional[Coverage],
):
    def exception_handler(e: Exception) -> None:
        for ctx_manager in ctx_managers:
            ctx_manager.__exit__(None, None, None)
        ctx_managers.clear()

        err_child_conn.send(pickle.dumps(sys.exc_info()))
        finished_event.set()

        try:
            attach: bool = err_child_conn.recv()
            if attach:
                sys.stdin = os.fdopen(0)
                attach_debugger(e)
        finally:
            finished_event.set()

    ctx_managers = []

    pickling_support.install()
    random.seed(random_seed)

    chain_process = _setup(port, network_id)

    set_exception_handler(exception_handler)

    start = time.perf_counter()
    while True:
        gen = None
        try:
            gen = default_chain.connect(f"http://localhost:{port}")
            gen.__enter__()
            break
        except (ConnectionRefusedError, URLError):
            if gen is not None:
                gen.__exit__(None, None, None)
            sleep(0.1)
            if time.perf_counter() - start > 10:
                raise

    try:
        if tee:
            ctx_managers.append(StdoutTee(log_file))
            ctx_managers.append(StderrTee(log_file))
        else:
            logging.basicConfig(filename=log_file)
            f = open(log_file, "w")
            ctx_managers.append(f)
            ctx_managers.append(redirect_stdout(f))
            ctx_managers.append(redirect_stderr(f))

        for ctx_manager in ctx_managers:
            ctx_manager.__enter__()

        _run_core(
            fuzz_test,
            index,
            random_seed,
            finished_event,
            err_child_conn,
            cov_child_conn,
            coverage,
        )
    except Exception:
        pass
    finally:
        for ctx_manager in ctx_managers:
            ctx_manager.__exit__(None, None, None)

        gen.__exit__(None, None, None)
        with log_file.open("a") as f, redirect_stdout(f), redirect_stderr(f):
            chain_process.kill()


def _compute_coverage_per_function(
    ide_cov: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, int]:
    funcs_cov = {}
    fn_names = []
    for path_rec in ide_cov.values():
        fn_names.extend([rec["name"] for rec in path_rec if rec["coverageHits"] > 0])

    for fn_path, path_rec in ide_cov.items():
        for fn_rec in path_rec:
            if fn_rec["coverageHits"] == 0:
                continue
            if fn_names.count(fn_rec["name"]) > 1:
                funcs_cov[f"{fn_path}:{fn_rec['name']}"] = fn_rec["coverageHits"]
            else:
                funcs_cov[fn_rec["name"]] = fn_rec["coverageHits"]

    return funcs_cov


def fuzz(
    config: WokeConfig,
    fuzz_test: types.FunctionType,
    process_count: int,
    seeds: Iterable[bytes],
    logs_dir: Path,
    passive: bool,
    network_id: str,
    cov_proc_num: int,
    verbose_coverage: bool,
):
    random_seeds = list(seeds)
    if len(random_seeds) < process_count:
        for i in range(process_count - len(random_seeds)):
            random_seeds.append(os.urandom(8))

    if cov_proc_num != 0:
        empty_coverage = Coverage()
    else:
        empty_coverage = None
    processes = dict()
    for i, seed in zip(range(process_count), random_seeds):
        console.print(f"Using seed '{seed.hex()}' for process #{i}")
        finished_event = multiprocessing.Event()
        err_parent_conn, err_child_con = multiprocessing.Pipe()
        cov_queue = multiprocessing.Queue()

        log_path = logs_dir / sanitize_filename(
            f"{fuzz_test.__module__}.{fuzz_test.__name__}_{i}.ansi"
        )

        proc_cov = copy.deepcopy(empty_coverage) if i < cov_proc_num else None

        p = multiprocessing.Process(
            target=_run,
            args=(
                fuzz_test,
                i,
                8545 + i,
                seed,
                log_path,
                passive and i == 0,
                finished_event,
                err_child_con,
                cov_queue,
                network_id,
                proc_cov,
            ),
        )
        processes[i] = (p, finished_event, err_parent_conn, cov_queue)
        p.start()

    with rich.progress.Progress(
        rich.progress.SpinnerColumn(finished_text="[green]⠿"),
        "[progress.description][yellow]{task.description}, "
        "[green]{task.fields[thr_rem]}[yellow] "
        "processes remaining{task.fields[coverage_info]}",
    ) as progress:
        exported_coverages: Dict[
            int, Dict[Path, Dict[IdePosition, IdeFunctionCoverageRecord]]
        ] = {}
        exported_coverages_per_trans: Dict[
            int, Dict[Path, Dict[IdePosition, IdeFunctionCoverageRecord]]
        ] = {}

        if passive:
            progress.stop()
        task = progress.add_task(
            "Fuzzing", thr_rem=len(processes), coverage_info="", total=1
        )

        while len(processes):
            to_be_removed = []
            for i, (p, e, err_parent_conn, cov_queue) in processes.items():
                finished = e.wait(0.125)
                if finished:
                    to_be_removed.append(i)

                    exception_info = err_parent_conn.recv()
                    if exception_info is not None:
                        exception_info = pickle.loads(exception_info)

                    if exception_info is not None:
                        if not passive or i == 0:
                            tb = Traceback.from_exception(
                                exception_info[0], exception_info[1], exception_info[2]
                            )

                            if not passive:
                                progress.stop()

                            console.print(tb)
                            console.print(
                                f"Process #{i} failed with an exception above."
                            )

                            attach = None
                            while attach is None:
                                response = input(
                                    "Would you like to attach the debugger? [y/n] "
                                )
                                if response == "y":
                                    attach = True
                                elif response == "n":
                                    attach = False
                        else:
                            attach = False

                        e.clear()
                        err_parent_conn.send(attach)
                        e.wait()
                        if not passive:
                            progress.start()

                    progress.update(task, thr_rem=len(processes) - len(to_be_removed))
                    if i == 0:
                        progress.start()
                while not cov_queue.empty():
                    try:
                        exported_coverage = cov_queue.get()
                        if not cov_queue.empty():
                            continue
                        (
                            exported_coverages[i],
                            exported_coverages_per_trans[i],
                        ) = exported_coverage
                    except EOFError:
                        pass
                    res = export_merged_ide_coverage(list(exported_coverages.values()))
                    res_per_trans = export_merged_ide_coverage(
                        list(exported_coverages_per_trans.values())
                    )
                    if res:
                        with open("woke-coverage.cov", "w") as f:
                            f.write(json.dumps(res, indent=4, sort_keys=True))
                        with open("woke-coverage-per-trans.cov", "w") as f:
                            f.write(json.dumps(res_per_trans, indent=4, sort_keys=True))
                    cov_info = ""
                    if not passive and verbose_coverage:
                        cov_info = "\n[dark_goldenrod]" + "\n".join(
                            [
                                f"{fn_name}: [green]{fn_calls}[dark_goldenrod]"
                                for (fn_name, fn_calls) in sorted(
                                    _compute_coverage_per_function(res).items(),
                                    key=lambda x: x[1],
                                    reverse=True,
                                )
                            ]
                        )
                    progress.update(task, coverage_info=cov_info)
                if finished:
                    cov_queue.close()

            for i in to_be_removed:
                processes.pop(i)

        progress.update(task, description="Finished", completed=1)

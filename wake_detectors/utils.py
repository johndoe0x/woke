def generate_detector_uri(name: str, version: str, anchor: str = "") -> str:
    uri = f"https://ackee.xyz/wake/docs/{version}/static-analysis/detectors/{name}"
    if anchor:
        uri += f"#{anchor}"
    return uri

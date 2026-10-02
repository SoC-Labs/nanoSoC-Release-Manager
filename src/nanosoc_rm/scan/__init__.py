"""Bundle and tree scanner (interfaces.md section 11; D11, D2): `scan_dir`, `scan_paths`, `ScanResult` here;
the rules and their invented specimens in nanosoc_rm.scan.rules. Engine: nanosoc_rm.scan.scan."""
from nanosoc_rm.scan.scan import MAX_FILE_BYTES, UNSCANNABLE, ScanResult, scan_dir, scan_paths, scan_texts, scan_tree

__all__ = ["ScanResult", "scan_dir", "scan_paths", "scan_tree", "scan_texts", "MAX_FILE_BYTES", "UNSCANNABLE"]

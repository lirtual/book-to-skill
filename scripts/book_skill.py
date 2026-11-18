#!/usr/bin/env python3
"""Private, review-first adapter around a pinned upstream book-to-skill extractor.

No book content, candidate, or extracted text is sent to a remote service.
This program never installs a generated skill.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


UPSTREAM_COMMIT = "e7bb659d3261491c8b28793dcb9102fa64a46c22"
REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TREE_BYTES = 20 * 1024 * 1024
SAFE_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,60}$")
SUSPICIOUS = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", re.I),
    re.compile(r"(?:disregard\s+(?:the\s+)?(?:system|developer)|you\s+are\s+now)", re.I),
    re.compile(r"(?:<\s*/?system\b|<\|im_start\|>|\[INST\])", re.I),
    re.compile(r"(?:curl|wget|upload|transmit).{0,90}(?:secret|credential|api[_-]?key|\.env)", re.I),
    re.compile(r"(?:secret|credential|api[_-]?key|\.env).{0,90}(?:curl|wget|upload|transmit)", re.I),
)


class WorkflowError(ValueError):
    pass


def write_json(path: Path, value: dict) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temp_path = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read JSON: {path}") from exc
    if not isinstance(result, dict):
        raise WorkflowError(f"Expected JSON object: {path}")
    return result


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_location(workspace: Path) -> Path:
    workspace = workspace.expanduser().resolve()
    if workspace == REPO_ROOT or REPO_ROOT in workspace.parents:
        raise WorkflowError("Private workspace must be outside this public repository")
    if not workspace.is_absolute():
        raise WorkflowError("Private workspace needs an absolute path")
    return workspace


def check_book(book: Path) -> Path:
    book = book.expanduser().resolve(strict=True)
    if book == REPO_ROOT or REPO_ROOT in book.parents:
        raise WorkflowError("Original books must not be placed inside the public repository")
    if not book.is_file():
        raise WorkflowError("Book must be a readable file")
    return book


def check_upstream_pin() -> None:
    try:
        info = importlib.metadata.distribution("book-to-skill").read_text("direct_url.json")
        origin = json.loads(info or "{}")
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError) as exc:
        raise WorkflowError("Pinned extractor missing. Install requirements.txt in an isolated venv") from exc
    vcs = origin.get("vcs_info", {})
    url = origin.get("url", "").removesuffix(".git")
    if (url != "https://github.com/virgiliojr94/book-to-skill"
            or vcs.get("vcs") != "git" or vcs.get("commit_id") != UPSTREAM_COMMIT):
        raise WorkflowError("Upstream extractor revision/source does not match the approved pin")


def get_body(corpus: str) -> str:
    """Remove the upstream single-source banner without dropping book characters."""
    lines = corpus.splitlines(keepends=True)
    if len(lines) < 5 or lines[0].strip() != "=" * 80 or not lines[1].startswith("SOURCE: "):
        raise WorkflowError("Unexpected upstream full_text.txt header")
    if lines[2].strip() != "=" * 80 or lines[3].strip():
        raise WorkflowError("Unexpected upstream source separator")
    return "".join(lines[4:])


def chapter_spans(body: str, source: dict) -> tuple[list[tuple[int, int, str]], str]:
    """Only treat the upstream embedded-outline ranges as authoritative."""
    ranges = source.get("chapter_ranges")
    if source.get("chapters_method") != "outline" or not isinstance(ranges, list) or not ranges:
        return [(0, len(body), "Ordered extracted text")], "ordered-blocks"
    lines = body.splitlines(keepends=True)
    positions = [0]
    for line in lines:
        positions.append(positions[-1] + len(line))
    valid = []
    try:
        for item in ranges:
            first = item["start_line"]
            last = item["end_line"]
            title = item["title"]
            if (not isinstance(first, int) or not isinstance(last, int)
                    or first < 1 or last < first or last > len(lines)
                    or not isinstance(title, str) or not title.strip()):
                raise ValueError("bad range")
            valid.append((positions[first - 1], positions[last], title))
    except (KeyError, TypeError, ValueError):
        return [(0, len(body), "Ordered extracted text")], "ordered-blocks"
    spans = []
    cursor = 0
    for start, end, title in valid:
        if start < cursor:
            return [(0, len(body), "Ordered extracted text")], "ordered-blocks"
        if cursor < start:
            spans.append((cursor, start, "Unmapped extracted text"))
        spans.append((start, end, title))
        cursor = end
    if cursor < len(body):
        spans.append((cursor, len(body), "Unmapped extracted text"))
    return spans, "outline-with-gaps"


def slice_spans(spans: list[tuple[int, int, str]], max_chars: int):
    for start, end, section in spans:
        cursor = start
        while cursor < end:
            stop = min(cursor + max_chars, end)
            yield cursor, stop, section
            cursor = stop


def index(book: Path, workspace: Path, max_chars: int) -> dict:
    if max_chars < 1 or max_chars > 100_000:
        raise WorkflowError("max-chars must be in [1, 100000]")
    workspace = check_location(workspace)
    if (workspace / "plan.json").exists():
        raise WorkflowError("Plan already exists; preserve existing review progress")
    book = check_book(book)
    meta = read_json(workspace / "extracted" / "metadata.json")
    output = workspace / "extracted"
    if (Path(meta.get("workdir", "")).resolve() != output
            or meta.get("total_sources") != 1
            or meta.get("extraction_mode") not in ("text", "technical")):
        raise WorkflowError("Upstream extraction metadata does not match this workspace")
    sources = meta.get("sources", [])
    if len(sources) != 1 or not isinstance(sources[0], dict):
        raise WorkflowError("V1 processes exactly one book per review")
    source = sources[0]
    if (Path(source.get("source_file", "")).resolve() != book
            or source.get("sha256") != sha256_bytes(book.read_bytes())):
        raise WorkflowError("Original book path/fingerprint differs from extracted source")
    corpus = (output / "full_text.txt").read_text(encoding="utf-8")
    body = get_body(corpus)
    if not body.strip():
        raise WorkflowError("Extracted text is empty")
    spans, segmentation = chapter_spans(body, source)
    parts = list(slice_spans(spans, max_chars))
    if not parts or parts[0][0] != 0 or parts[-1][1] != len(body) or any(
            previous[1] != following[0] for previous, following in zip(parts, parts[1:])):
        raise WorkflowError("Coverage partition is incomplete")
    block_dir = workspace / "blocks"
    block_dir.mkdir(mode=0o700)
    blocks = []
    for i, (start, end, section) in enumerate(parts, start=1):
        identifier = f"B{i:04d}"
        filename = f"blocks/{identifier}.txt"
        content = body[start:end]
        with (workspace / filename).open("x", encoding="utf-8") as handle:
            os.chmod(workspace / filename, 0o600)
            handle.write(content)
        blocks.append({
            "id": identifier, "file": filename, "section": section,
            "start_char": start, "end_char": end, "sha256": sha256_bytes(content.encode("utf-8")),
            "status": "pending", "note": "",
        })
    extraction_unknowns = ["Original pages/sections versus extracted text completeness is unverified"]
    if source.get("images_dropped"):
        extraction_unknowns.append(f"Images not extracted: {source['images_dropped']}")
    if source.get("fallback_reason"):
        extraction_unknowns.append(f"Extraction fallback: {source['fallback_reason']}")
    plan = {
        "version": 1, "upstream_commit": UPSTREAM_COMMIT, "book_file": str(book),
        "book_sha256": source["sha256"], "extraction_mode": meta["extraction_mode"],
        "segmentation": segmentation, "extracted_chars": len(body),
        "extracted_sha256": sha256_bytes(body.encode("utf-8")),
        "extraction_unknowns": extraction_unknowns, "blocks": blocks,
    }
    write_json(workspace / "plan.json", plan)
    return {"workspace": str(workspace), "blocks": len(blocks), "segmentation": segmentation,
            "extracted_chars": len(body), "source_completeness": "Unknown"}


def prepare(args: argparse.Namespace) -> dict:
    if not args.rights_confirmed:
        raise WorkflowError("Explicit --rights-confirmed is required before reading the book")
    book = check_book(Path(args.book))
    workspace = check_location(Path(args.workspace))
    if workspace.exists():
        raise WorkflowError("Choose a new private workspace to avoid overwriting prior work")
    check_upstream_pin()
    workspace.mkdir(mode=0o700)
    extracted = workspace / "extracted"
    env = {**os.environ, "BOOK_SKILL_WORKDIR": str(extracted), "PYTHONDONTWRITEBYTECODE": "1"}
    print(f"Private workspace: {workspace}", file=sys.stderr)
    print("On error, private files are retained here for inspection/recovery.", file=sys.stderr)
    cmd = [sys.executable, "-m", "book_to_skill", str(book),
           "--mode", args.mode, "--install-missing", "no"]
    completed = subprocess.run(cmd, env=env, cwd=workspace, text=True, capture_output=True)
    if completed.returncode:
        raise WorkflowError(f"Upstream extraction failed (exit {completed.returncode}): "
                            f"{completed.stderr[-1500:]}")
    return index(book, workspace, args.max_chars)


def mark(workspace: Path, identifier: str, status: str, note: str) -> dict:
    workspace = check_location(workspace)
    plan = read_json(workspace / "plan.json")
    if len(note) > 500:
        raise WorkflowError("Review note must be <= 500 characters")
    selected = next((part for part in plan["blocks"] if part["id"] == identifier), None)
    if selected is None:
        raise WorkflowError(f"Unknown block ID: {identifier}")
    selected["status"] = status
    selected["note"] = note
    write_json(workspace / "plan.json", plan)
    return {"block": identifier, "status": status}


def candidate_files(root: Path) -> list[str]:
    errors = []
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            errors.append(f"{relative}: symlink is not permitted")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            errors.append(f"{relative}: not a regular file")
            continue
        if path.name.lower() in {".env", "credentials.json"} or path.suffix.lower() in {
                ".pem", ".key", ".p12", ".pfx"}:
            errors.append(f"{relative}: credential-like file must not be packaged")
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
            errors.append(f"{relative}: file or total scan limit exceeded")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"{relative}: binary/non-UTF-8 file requires manual review")
            continue
        if any(pattern.search(text) for pattern in SUSPICIOUS):
            errors.append(f"{relative}: suspicious instruction/authority or exfiltration pattern")
        if any(ord(char) in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x202E) for char in text):
            errors.append(f"{relative}: invisible or direction-changing Unicode")
    return errors


def assess_candidate(workspace: Path, candidate: dict, checked_ids: set[str]) -> list[str]:
    slug = candidate.get("slug", "")
    if not isinstance(slug, str) or not SAFE_SLUG.fullmatch(slug):
        return ["Candidate has invalid slug"]
    errors = []
    for key in ("purpose", "trigger", "boundaries", "evidence", "positive_task", "negative_task",
                "positive_result", "negative_result"):
        if not isinstance(candidate.get(key), str) or not candidate[key].strip():
            errors.append(f"{slug}: missing {key}")
    if not isinstance(candidate.get("steps"), list) or not candidate["steps"] or not all(
            isinstance(step, str) and step.strip() for step in candidate["steps"]):
        errors.append(f"{slug}: missing executable steps")
    source_ids = candidate.get("sources", [])
    if not isinstance(source_ids, list) or not source_ids:
        errors.append(f"{slug}: sources required")
    else:
        for identifier in source_ids:
            if not isinstance(identifier, str) or identifier not in checked_ids:
                errors.append(f"{slug}: source block {identifier} is missing or unchecked")
    root = workspace / "candidates" / slug
    skill = root / "SKILL.md"
    if root.is_symlink() or not root.is_dir() or skill.is_symlink() or not skill.is_file():
        errors.append(f"{slug}: isolated SKILL.md missing or unsafe path")
    else:
        try:
            body = skill.read_text(encoding="utf-8")
        except UnicodeError:
            body = ""
        if (not body.startswith("---\n") or "\n---\n" not in body[4:]
                or not re.search(r"(?m)^name:\s*" + re.escape(slug) + r"\s*$", body)
                or not re.search(r"(?m)^description:\s*\S", body)):
            errors.append(f"{slug}: SKILL.md frontmatter invalid")
        errors.extend(f"{slug}/{err}" for err in candidate_files(root))
    for key in ("validation_status", "security_review"):
        if candidate.get(key) not in ("Pass", "Partial", "Fail", "Unknown"):
            errors.append(f"{slug}: {key} must be explicitly reported")
    return errors


def report(workspace: Path) -> tuple[dict, int]:
    workspace = check_location(workspace)
    plan = read_json(workspace / "plan.json")
    blocks = plan.get("blocks", [])
    findings = []
    statuses = {"pending": 0, "checked": 0, "failed": 0}
    checked_ids = set()
    reconstructed = []
    for item in blocks:
        identifier = item["id"]
        file = workspace / item["file"]
        if file.is_symlink() or not file.is_file():
            findings.append(f"{identifier}: missing or symlinked source block")
            continue
        content = file.read_text(encoding="utf-8")
        if sha256_bytes(content.encode("utf-8")) != item["sha256"]:
            findings.append(f"{identifier}: content changed after indexing")
        reconstructed.append(content)
        status = item.get("status", "pending")
        if status not in statuses:
            findings.append(f"{identifier}: invalid review status")
        else:
            statuses[status] += 1
            if status == "checked":
                checked_ids.add(identifier)
    if not blocks or sha256_bytes("".join(reconstructed).encode("utf-8")) != plan["extracted_sha256"]:
        findings.append("Source block coverage or order does not match extracted text")
    if statuses["failed"]:
        findings.append("At least one source block failed review")
    candidates_file = workspace / "candidates.json"
    candidates = []
    if candidates_file.is_file():
        data = read_json(candidates_file)
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            candidates = []
            findings.append("candidates must be an array")
        if not candidates and not str(data.get("no_candidate_reason", "")).strip():
            findings.append("Zero-candidate conclusion requires an explanation")
    else:
        data = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            findings.append("Candidate must be an object")
        else:
            findings.extend(assess_candidate(workspace, candidate, checked_ids))
    duplicate_slugs = [c.get("slug") for c in candidates if isinstance(c, dict)
                       and isinstance(c.get("slug"), str)]
    if len(duplicate_slugs) != len(set(duplicate_slugs)):
        findings.append("Duplicate candidate slug")
    if findings:
        gate = "Fail"
    elif statuses["pending"] or not candidates_file.is_file():
        gate = "Partial"
    elif any(c.get("validation_status") != "Pass" or c.get("security_review") != "Pass"
             for c in candidates):
        gate = "Partial"
    else:
        gate = "Pass"
    result = {
        "review_gate": gate, "source_completeness": "Unknown",
        "extraction_unknowns": plan["extraction_unknowns"],
        "extracted_chars": plan["extracted_chars"], "blocks": statuses,
        "candidates": len(candidates), "findings": findings,
        "installation": "Not authorized / not performed",
        "host_validation": {"ChatGPT": "Unknown", "Codex": "Unknown"},
    }
    return result, 0 if gate == "Pass" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="Extract one user-authorized book into an isolated workspace")
    p.add_argument("--book", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--mode", choices=("text", "technical"), default="text")
    p.add_argument("--rights-confirmed", action="store_true")
    p.add_argument("--max-chars", type=int, default=8000)
    p = commands.add_parser("index", help="Resume indexing an already-extracted private workspace")
    p.add_argument("--book", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--max-chars", type=int, default=8000)
    p = commands.add_parser("mark", help="Record a reviewed/failed block after actually reading it")
    p.add_argument("--workspace", required=True)
    p.add_argument("--block", required=True)
    p.add_argument("--status", required=True, choices=("checked", "failed"))
    p.add_argument("--note", default="")
    p = commands.add_parser("report", help="Validate private candidates, scan files, and report gates")
    p.add_argument("--workspace", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args)
        elif args.command == "index":
            result = index(Path(args.book), Path(args.workspace), args.max_chars)
        elif args.command == "mark":
            result = mark(Path(args.workspace), args.block, args.status, args.note)
        else:
            result, code = report(Path(args.workspace))
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return code
    except (WorkflowError, OSError, UnicodeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

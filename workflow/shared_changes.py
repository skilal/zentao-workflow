"""共享目录的文件版本、编辑记录和可审查片段；记录独立落盘。"""

from collections import defaultdict
from contextlib import ExitStack
from difflib import unified_diff
from hashlib import sha256
from json import dumps, loads
from pathlib import Path

from workflow.gitops import checkout_snapshot, ensure_baseline, git, repository_lock, safe_file
from workflow.diff_index import line_opcodes, occurrence_counts
from workflow.settings import WorkflowError


SCOPE_SCHEMA = {"type": "array", "items": {"type": "object", "properties": {
    "repository": {"type": "string"},
    "include": {"type": "array", "items": {"type": "string"}},
    "exclude": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["id", "reason"], "additionalProperties": False}}},
    "required": ["repository", "include", "exclude"], "additionalProperties": False}}


def digest(value):
    return sha256(dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def save_record(path, record):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_record(repo):
    return loads(Path(repo["change_record"]).read_text(encoding="utf-8"))


def blob_directory(repo):
    return Path(repo["change_record"]).parent / "blobs"


def store_blob(repo, data):
    if data is None:
        return None
    directory = blob_directory(repo)
    directory.mkdir(exist_ok=True)
    key = sha256(data).hexdigest()
    path = directory / key
    if not path.exists():
        path.write_bytes(data)
    return key


def read_blob(repo, key):
    return (blob_directory(repo) / key).read_bytes() if key is not None else None


def file_version(path):
    return sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def head_file(repo, relative, revision="HEAD"):
    entry = git(repo["path"], "--literal-pathspecs", "ls-tree", "-z", revision, "--", relative)
    if not entry:
        return None, "100644"
    metadata = entry.split("\t", 1)[0].split()
    if metadata[0] not in {"100644", "100755"}:
        raise WorkflowError(f"共享差异只支持普通文件：{relative}")
    return git(repo["path"], "cat-file", "blob", metadata[2], binary=True), metadata[0]


def initialize(store, state, repo):
    directory = store.directory(state["id"]) / "changes"
    directory.mkdir(exist_ok=True)
    key = sha256((str(Path(repo["path"]).resolve()).casefold() + "\0" + repo["name"] + "\0" + repo["branch"]).encode()).hexdigest()[:16]
    path = directory / f"{state.get('delivery_round', 1)}-{key}.json"
    repo["change_record"] = str(path)
    if path.exists():
        return
    with repository_lock(repo["path"]):
        snapshot = checkout_snapshot(repo["path"])
        files = {}
        for relative in snapshot["paths"]:
            file = safe_file(repo, relative)
            files[relative] = store_blob(repo, file.read_bytes() if file.is_file() else None)
        record = {"version": 1, "session": state["id"], "round": state.get("delivery_round", 1),
                  "repository": repo["name"], "path": str(Path(repo["path"]).resolve()), "branch": repo["branch"],
                  "head": git(repo["path"], "rev-parse", "HEAD"), "initial": files,
                  "initial_snapshot": snapshot, "edits": [], "reads": {}, "review": None}
        save_record(path, record)


def initialize_session(store, state):
    for repo in state.get("repos", []):
        if repo.get("change_record"):
            record = load_record(repo)
            if record["path"].casefold() != str(Path(repo["path"]).resolve()).casefold() or record["branch"] != repo["branch"]:
                repo.pop("change_record")
                repo.pop("commit_scope", None)
        if not repo.get("change_record"):
            initialize(store, state, repo)


def lines(data):
    return data.decode("utf-8").replace("\r\n", "\n").splitlines(keepends=True) if data is not None else []


def edits(before, after, *, split_insertions=True):
    """插入和等长替换按行拆分，避免相邻需求粘成一个 hunk。"""
    result = []
    for tag, a, b, c, d in line_opcodes(before, after):
        if tag == "equal":
            continue
        if tag == "insert" and split_insertions:
            result.extend((a, a, position, position + 1) for position in range(c, d))
        elif tag == "replace" and b - a == d - c:
            result.extend((a + offset, a + offset + 1, c + offset, c + offset + 1)
                          for offset in range(b - a))
        elif tag == "replace" and split_insertions:
            # 行数不同不猜测行的对应关系；独立展示删除与插入，由修改记录确定归属。
            result.extend((position, position + 1, c, c) for position in range(a, b))
            result.extend((b, b, position, position + 1) for position in range(c, d))
        else:
            result.append((a, b, c, d))
    return result


def journals(repo):
    current = load_record(repo)
    root = Path(repo["change_record"]).parents[2]
    records = []
    for path in root.glob("*/changes/*.json"):
        record = loads(path.read_text(encoding="utf-8"))
        if (record.get("version") == 1 and record["path"].casefold() == current["path"].casefold()
                and record["branch"] == current["branch"]):
            records.append(({**repo, "change_record": str(path)}, record))
    return current, records


def line_owners(relative, current_lines, records):
    owners = defaultdict(set)
    deleted = defaultdict(set)
    for other, record in records:
        for edit in record["edits"]:
            if edit["path"] != relative:
                continue
            before, after = lines(read_blob(other, edit["before"])), lines(read_blob(other, edit["after"]))
            changed = edits(before, after)
            mapping = {}
            for tag, a, b, c, d in line_opcodes(after, current_lines):
                if tag == "equal":
                    mapping.update((a + offset, c + offset) for offset in range(b - a))
            for a, b, c, d in changed:
                for position in range(c, d):
                    if position in mapping:
                        owners[mapping[position]].add(record["session"] + "#" + str(record["round"]))
                if a != b:
                    anchor = mapping[c] if c in mapping else mapping[c - 1] + 1 if c - 1 in mapping else 0 if not current_lines else None
                    if anchor is not None:
                        deleted[("".join(before[a:b]), anchor)].add(record["session"] + "#" + str(record["round"]))
    return owners, deleted


def candidates(repo, paths=None):
    ensure_baseline(repo)
    record, records = journals(repo)
    if paths is None:
        raw = git(repo["path"], "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", "HEAD", binary=True)
        raw += git(repo["path"], "ls-files", "--others", "--exclude-standard", "-z", binary=True)
        paths = {part.decode("utf-8") for part in raw.split(b"\0") if part}
    result = []
    for relative in sorted(paths):
        path = safe_file(repo, relative)
        before_data, mode = head_file(repo, relative)
        after_data = path.read_bytes() if path.is_file() else None
        if before_data == after_data:
            continue
        binary = (before_data is not None and b"\0" in before_data) or (after_data is not None and b"\0" in after_data)
        try:
            before, after = lines(before_data), lines(after_data)
        except UnicodeError:
            binary = True
        if binary:
            identifier = digest([relative, store_blob(repo, before_data), store_blob(repo, after_data)])[:24]
            result.append({"id": identifier, "path": relative, "old_start": 0, "old_end": 0, "new_start": 0, "new_end": 0,
                           "old": "", "new": "", "mode": mode, "kind": "二进制", "source": "待判断", "owners": [],
                           "context": identifier, "binary_blob": store_blob(repo, after_data)})
            continue
        owners, deleted = line_owners(relative, after, records)
        # 新增、删除作为完整文件处理，不能提交残缺的创建或删除。
        ranges = [(0, len(before), 0, len(after))] if before_data is None or after_data is None else edits(before, after)
        replacement_windows = [(a, b, c, d) for tag, a, b, c, d in line_opcodes(before, after) if tag == "replace"]
        occurrences = occurrence_counts(after, [(c, d) for a, b, c, d in ranges])
        removed_occurrences = iter(occurrence_counts(before, [(a, b) for a, b, c, d in ranges if c == d]))
        for a, b, c, d in ranges:
            if c == d:
                deleted.setdefault(("".join(before[a:b]), c), set())
        deleted_positions = defaultdict(list)
        for (text, position), ownership in deleted.items():
            deleted_positions[text].append((position, ownership))
        for (a, b, c, d), occurrence in zip(ranges, occurrences):
            old, new = "".join(before[a:b]), "".join(after[c:d])
            signature = digest([relative, old, new])
            context = [after[max(0, c - 2):c], after[d:d + 2]]
            origin = set().union(*(owners.get(index, ()) for index in range(c, d))) if c != d else deleted[(old, c)]
            if c == d:
                for first, last, start, end in replacement_windows:
                    if first <= a < b <= last:
                        origin.update(owner for position, ownership in deleted_positions.get(old, ())
                                      if start <= position <= end for owner in ownership)
            identifier = digest([signature, context, occurrence])[:24]
            if c == d:
                removed_context = [before[max(0, a - 2):a], before[b:b + 2]]
                removed_occurrence = next(removed_occurrences)
                identifier = digest([signature, context, removed_context, removed_occurrence])[:24]
            origin.update(other_record["session"] + "#" + str(other_record["round"])
                          for _, other_record in records if identifier in other_record.get("scope_claims", []))
            origin.difference_update(other_record["session"] + "#" + str(other_record["round"])
                                     for _, other_record in records if identifier in other_record.get("excluded_claims", []))
            sessions = {owner.rsplit("#", 1)[0] for owner in origin}
            source = "本轮" if record["session"] + "#" + str(record["round"]) in origin else "历史任务" if sessions == {record["session"]} else "其他会话" if origin else "待判断"
            if len(sessions) > 1:
                source = "归属重叠"
            result.append({"id": identifier, "path": relative, "old_start": a, "old_end": b,
                           "new_start": c, "new_end": d, "old": old, "new": new, "mode": mode,
                           "kind": "新增" if before_data is None else "删除" if after_data is None else "修改",
                           "source": source, "owners": sorted(sessions), "context": digest(context)})
    return result


def own_fragments(repo, fragments=None):
    fragments = candidates(repo) if fragments is None else fragments
    selected = repo.get("commit_scope")
    if selected is not None:
        lookup = {fragment["id"]: fragment for fragment in fragments}
        if any(identifier not in lookup for identifier in selected):
            raise WorkflowError("本轮已审查差异已变化或已被提交，请重新 Review。")
        return [lookup[identifier] for identifier in selected]
    return [fragment for fragment in fragments if fragment["source"] == "本轮"
            or (fragment["path"] in repo.get("review_paths", []) and fragment["source"] in {"历史任务", "待判断"})]


def requested_manifest(repo, paths):
    fragments = candidates(repo)
    selected = [fragment["id"] for fragment in fragments if fragment["path"] in paths
                and fragment["source"] not in {"其他会话", "归属重叠"}]
    return fragment_manifest({**repo, "commit_scope": selected}, fragments)


def fragment_manifest(repo, fragments=None):
    grouped = defaultdict(list)
    for fragment in own_fragments(repo, fragments):
        grouped[fragment["path"]].append([fragment["id"], fragment["context"]])
    return {repo["name"] + "/" + path: digest(sorted(values)) for path, values in grouped.items()}


def snapshot_review(repo):
    with repository_lock(repo["path"]):
        record = load_record(repo)
        fragments = candidates(repo)
        record["review"] = {fragment["id"]: fragment for fragment in fragments}
        record["review_reads"] = {}
        save_record(repo["change_record"], record)
        return fragment_manifest(repo, fragments)


def review_reads_valid(repo):
    record = load_record(repo)
    for relative, reads in record.get("review_reads", {}).items():
        path = safe_file(repo, relative)
        exists = path.is_file()
        current = lines(path.read_bytes() if exists else None)
        for read in reads:
            if exists != read.get("exists", True):
                raise WorkflowError(f"Review 所依据的文件存在状态已变化，请重新 Review：{relative}")
            text = "".join(current[read["start"]:read["end"]])
            if digest(text) != read["digest"]:
                expected = read_blob(repo, read["blob"]).decode("utf-8")
                if expected and "".join(current).count(expected) == 1:
                    continue
                raise WorkflowError(f"Review 所依据的代码片段已变化，请重新 Review：{relative}")


def remember_read(repo, relative, start, count, review=False, content=None):
    if not repo.get("change_record"):
        return
    with repository_lock(repo["path"]):
        record = load_record(repo)
        path = safe_file(repo, relative)
        content = path.read_bytes() if content is None and path.is_file() else content
        record["reads"][relative] = sha256(content).hexdigest() if content is not None else "missing"
        if review:
            text = lines(content)
            a, b = start - 1, min(len(text), start - 1 + count)
            record.setdefault("review_reads", {}).setdefault(relative, []).append(
                {"start": a, "end": b, "digest": digest("".join(text[a:b])),
                 "exists": content is not None,
                 "blob": store_blob(repo, "".join(text[a:b]).encode("utf-8"))})
        save_record(repo["change_record"], record)


def validate_write(repo, relative, expected_version):
    ensure_baseline(repo)
    path = safe_file(repo, relative)
    if not expected_version or file_version(path) != expected_version:
        raise WorkflowError(f"文件版本已变化或缺少版本，请重新读取后修改：{relative}")


def record_write(repo, relative, before, after):
    record = load_record(repo)
    record["edits"].append({"path": relative, "before": store_blob(repo, before), "after": store_blob(repo, after)})
    record["reads"][relative] = sha256(after).hexdigest() if after is not None else "missing"
    save_record(repo["change_record"], record)


def check_overlap(repo, relative, before, after):
    record, records = journals(repo)
    original, updated = lines(before), lines(after)
    owners, deleted = line_owners(relative, original, records)
    claimed = candidates(repo, {relative}) if any(other["session"] != record["session"] and other.get("scope_claims")
                                                for _, other in records) else []
    for a, b, c, d in edits(original, updated):
        foreign = set().union(*(owners[index] for index in range(a, b))) if a != b else set()
        if {owner.rsplit("#", 1)[0] for owner in foreign} - {record["session"]}:
            raise WorkflowError(f"修改片段与其他会话重叠，请先明确处理归属：{relative}")
        for fragment in claimed:
            if (fragment["path"] == relative and set(fragment["owners"]) - {record["session"]}
                    and max(a, fragment["new_start"]) < min(b, fragment["new_end"])):
                raise WorkflowError(f"修改片段与其他会话已确认范围重叠：{relative}")


def changes(repo):
    with repository_lock(repo["path"]):
        fragments = candidates(repo)
        record = load_record(repo)
        summaries = [{key: fragment[key] for key in ("id", "path", "kind", "source", "owners")}
                     | {"line": fragment["new_start"] + 1} for fragment in fragments]
        owned = own_fragments(repo, fragments)
        patch = "\n".join("".join(unified_diff(fragment["old"].splitlines(True), fragment["new"].splitlines(True),
                                                fromfile="a/" + fragment["path"], tofile="b/" + fragment["path"]))
                          for fragment in owned)
        return {"repository": repo["name"], "baseline": record["head"], "stat": f"本轮 {len(owned)} 个片段；候选 {len(fragments)} 个片段",
                "patch": patch, "fragments": summaries, "untracked": sorted({f["path"] for f in owned if f["kind"] == "新增"}),
                "note": "片段编号用于 commit_scope；待判断片段按需用 changes(fragment_ids) 阅读，其他会话片段不能纳入。"}


def fragment_details(repo, identifiers):
    lookup = {entry["id"]: entry for entry in candidates(repo)}
    if any(identifier not in lookup for identifier in identifiers):
        raise WorkflowError("差异片段已变化，请重新读取 changes。")
    return [{key: lookup[identifier][key] for key in ("id", "path", "kind", "source", "old", "new")}
            for identifier in identifiers]


def select_scope(repositories, selections):
    with ExitStack() as locks:
        for path in sorted({repo["path"] for repo in repositories}):
            locks.enter_context(repository_lock(path))
        return select_locked_scope(repositories, selections)


def select_locked_scope(repositories, selections):
    shared = {repo["name"]: repo for repo in repositories if repo.get("change_record")}
    if len(selections) != len(shared) or {selection["repository"] for selection in selections} != set(shared):
        raise WorkflowError("commit_scope 必须逐一覆盖本轮共享仓库。")
    prepared = []
    for selection in selections:
        repo = shared[selection["repository"]]
        record = load_record(repo)
        frozen = record.get("review") or {}
        current = {fragment["id"]: fragment for fragment in candidates(repo)}
        included = selection["include"]
        excluded = {entry["id"]: entry["reason"] for entry in selection["exclude"]}
        if (len(set(included)) != len(included) or len(excluded) != len(selection["exclude"])
                or set(included) & set(excluded) or any(not reason.strip() for reason in excluded.values())):
            raise WorkflowError("提交片段重复、状态冲突或缺少排除原因。")
        own = {identifier for identifier, fragment in frozen.items() if fragment["source"] == "本轮"
               or (fragment["path"] in repo.get("review_paths", []) and fragment["source"] in {"历史任务", "待判断"})}
        if not own.issubset(set(included) | set(excluded)):
            raise WorkflowError("本轮修改片段遗漏，请明确纳入或说明排除原因。")
        for identifier in [*included, *excluded]:
            if identifier not in frozen or identifier not in current:
                raise WorkflowError("Review 使用了失效或未知的差异片段。")
            if identifier in included:
                entry = current[identifier]
                if entry["source"] in {"其他会话", "归属重叠"}:
                    raise WorkflowError("不能提交其他会话或归属重叠的差异片段。")
                if entry["context"] != frozen[identifier]["context"]:
                    raise WorkflowError("Review 期间相关代码上下文已变化，请重新 Review。")
        review_reads_valid(repo)
        prepared.append((repo, list(included), list(excluded)))
    for repo, included, excluded in prepared:
        repo["commit_scope"] = included
        record = load_record(repo)
        record["scope_claims"] = included
        record["excluded_claims"] = excluded
        save_record(repo["change_record"], record)
    return {key: value for repo, _, _ in prepared for key, value in fragment_manifest(repo).items()}

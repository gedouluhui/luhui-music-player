"""Library scanner: walk folders, read tags, keep the DB in sync.

Incremental by design - a file whose size+mtime is unchanged is not re-parsed,
so re-scanning a 5k-track library takes well under a second.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import db
from .config import AUDIO_EXTS, normalize_path
from .metadata import cover_cache_trim, read_track_metadata

JUNK_DIRS = {
    "$recycle.bin", "system volume information", "#recycle", "@eadir", ".ds_store",
    ".git", ".idea", ".vscode", "node_modules", "appdata", "programdata", "windows",
    "$winreagent", "winhttpcache", "microsoft", "performance", "temp", "preferences",
}
HIDDEN_PREFIX = "."

_STATE_LOCK = threading.Lock()
# STATE 只有一个槽位，所以「同时只跑一个扫描」必须是硬约束。老写法是先查
# STATE["running"] 再开线程，而 running 要等线程里 _reset() 才置真 —— 这个空档里
# 第二个请求也能过检查，两个线程往同一个 STATE 里写，先结束的那个会把 running 清成
# False，调用方于是看到一次「done」而另一份还在走（run 16 的 rescan-flags-missing
# 就是这么被抢跑的：等扫描返回后失效条目还没标记，一秒后 purge 却删掉了它）。
# 用一把专门的锁把「判断 + 启动」合成一个原子动作，线程跑完再放开。
_SCAN_LOCK = threading.Lock()
STATE: Dict[str, Any] = {
    "running": False, "phase": "idle", "total": 0, "done": 0, "current": "",
    "added": 0, "updated": 0, "removed": 0, "skipped": 0, "excluded": 0, "errors": [],
    "started_at": 0.0, "finished_at": 0.0,
}


def state_snapshot() -> Dict[str, Any]:
    with _STATE_LOCK:
        s = dict(STATE)
        s["errors"] = list(s["errors"][-20:])
        return s


def _reset(total: int = 0) -> None:
    with _STATE_LOCK:
        STATE.update({
            "running": True, "phase": "walking", "total": total, "done": 0, "current": "",
            "added": 0, "updated": 0, "removed": 0, "skipped": 0, "excluded": 0, "errors": [],
            "started_at": time.time(), "finished_at": 0.0,
        })


def _set(**kw: Any) -> None:
    with _STATE_LOCK:
        STATE.update(kw)


def _note_error(msg: str) -> None:
    with _STATE_LOCK:
        errs = STATE["errors"]
        if not errs or errs[-1] != msg:
            errs.append(msg[:300])
        STATE["errors"] = errs[-200:]


def is_junk_dir(name: str) -> bool:
    low = name.lower()
    return low in JUNK_DIRS or low.startswith("$") or (low.startswith(HIDDEN_PREFIX) and low not in (".cache",))


def walk_audio(root: str, max_files: int = 200_000,
               blacklist: Optional[List[str]] = None) -> tuple[List[str], bool]:
    """收集 root 下的音频文件；blacklist 里的目录（含其子树）整个跳过。

    返回 ``(files, complete)``。``complete`` 为 False 表示这次遍历**没有看全**：
    有目录无权限、或命中 max_files 截断。调用方拿到不完整清单时绝不能拿它做
    「不在清单里的都标失效」的判定 —— 否则一次 U 盘掉线/权限错误就会把整库曲目
    打进 missing，收藏和统计跟着一起塌。

    命中黑名单时是在 dirpath 这一层直接 continue，所以「这个目录自己的文件」和
    「它下面的子目录」一起被跳过 —— 这正是用户点「不再扫描此文件夹」想要的效果。
    """
    found: List[str] = []
    seen_dirs = set()
    bl = [b for b in (blacklist or []) if b]
    complete = True

    def onerr(err: OSError) -> None:
        nonlocal complete
        complete = False
        _note_error(f"跳过无权限目录: {getattr(err, 'filename', err)}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerr, followlinks=False):
        # prune junk + symlink loops
        key = normalize_path(dirpath).lower()
        st = None
        try:
            st = os.stat(dirpath)
            dkey = (st.st_dev, st.st_ino)
        except Exception:
            dkey = None
        if dkey in seen_dirs and dkey is not None:
            dirnames[:] = []
            continue
        if dkey is not None:
            seen_dirs.add(dkey)
        if bl and any(db.covers(b, dirpath) for b in bl):
            dirnames[:] = []
            with _STATE_LOCK:
                STATE["excluded"] += 1
            continue
        dirnames[:] = [d for d in dirnames if not is_junk_dir(d)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            p = os.path.join(dirpath, fn)
            if p.startswith("\\\\?\\"):
                p = p[4:]
            found.append(p)
            if len(found) >= max_files:
                _note_error(f"曲库文件数超过上限 {max_files}，已截断")
                complete = False
                return found, complete
    return found, complete


def scan_library(library_id: Optional[int] = None, include_unsupported: bool = False,
                 force: bool = False) -> Dict[str, Any]:
    """Blocking scan. Call through scan_async() from HTTP handlers."""
    libs = db.list_libraries()
    targets = [l for l in libs if library_id is None or l["id"] == library_id]
    if not targets:
        return {"ok": False, "error": "没有可扫描的音乐文件夹", **state_snapshot()}

    _reset()
    bl = db.blacklist_dirs()
    try:
        grand = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
        total_files = 0
        per_lib: Dict[int, tuple[List[str], bool]] = {}
        for lib in targets:
            if not os.path.isdir(lib["path"]):
                _note_error(f"文件夹不存在: {lib['path']}")
                continue
            t0 = time.time()
            files, complete = walk_audio(lib["path"], blacklist=bl)
            per_lib[lib["id"]] = (files, complete)
            total_files += len(files)
            with _STATE_LOCK:
                STATE["total"] = total_files
            _set(current=f"已找到 {len(files)} 个音频文件 ({lib['name']}) · {time.time() - t0:.1f}s")
        known = _known_index()
        for lib in targets:
            entry = per_lib.get(lib["id"])
            if entry is None:
                continue
            files, complete = entry
            _ingest(lib, files, known, include_unsupported, grand, force)
            if not complete:
                # 残缺清单只能用来「加/更新」，不能用来判定谁不见了。
                _note_error(f"跳过失效标记: {lib['name']} 本轮遍历不完整（无权限或被截断）")
                continue
            if not files and not force and db.track_count(["library_id = ?"], [lib["id"]]) >= 10:
                # 盘没挂载、路径暂时不可见都会让本轮只有 0 个文件。整库 ≥10 首时
                # 这种「全空」更可能是环境问题而不是用户真删光了；force 可显式覆盖。
                _note_error(f"跳过失效标记: {lib['name']} 本轮扫描到 0 个文件（可能是磁盘未挂载）")
                continue
            _reconcile(lib, files, grand)
        db.set_meta("last_scan_at", time.time())
        # 派生封面只增不减；扫描收尾顺手把目录压回配额。失败绝不能让扫描报错。
        try:
            cover_cache_trim()
        except Exception:
            pass
        _set(phase="done", current="", **grand)
        STATE["running"] = False
        STATE["finished_at"] = time.time()
        return {"ok": True, **state_snapshot()}
    except Exception as e:  # never let a scan kill the server thread
        _note_error(f"扫描异常: {type(e).__name__}: {e}")
        _set(phase="error")
        with _STATE_LOCK:
            STATE["running"] = False
            STATE["finished_at"] = time.time()
        return {"ok": False, "error": str(e), **state_snapshot()}


def _known_index() -> Dict[str, tuple[float, float, int]]:
    out: Dict[str, tuple[float, float, int]] = {}
    for r in db.query("SELECT id, path, mtime, size FROM tracks"):
        out[r["path"].lower()] = (float(r["mtime"]), float(r["size"]), int(r["id"]))
    return out


def _ingest(lib: Dict[str, Any], files: List[str], known: Dict[str, tuple], include_unsupported: bool,
            grand: Dict[str, int], force: bool = False) -> None:
    now = time.time()
    # 攒够一批再一次 commit：以前每首都走 db.write()（各自 commit），首次导入几千首
    # 就是几千次 WAL 落盘，Windows 上这一步比读元数据本身还慢。
    stmts: List[tuple] = []

    def flush() -> None:
        if stmts:
            db.batch(list(stmts))
            stmts.clear()

    for i, path in enumerate(files):
        key = normalize_path(path).lower()
        try:
            st = os.stat(path)
        except Exception as e:
            grand["skipped"] += 1
            _note_error(f"无法读取: {path} ({e})")
            continue
        prev = known.get(key)
        unchanged = prev and abs(prev[0] - st.st_mtime) < 1e-6 and prev[1] == st.st_size
        if unchanged and not force:
            with _STATE_LOCK:
                STATE["done"] += 1
            continue
        try:
            meta = read_track_metadata(path)
        except Exception as e:
            grand["skipped"] += 1
            _note_error(f"解析失败: {path} ({type(e).__name__}: {e})")
            continue
        if not meta.get("title"):
            grand["skipped"] += 1
            continue
        meta["library_id"] = lib["id"]
        meta["path"] = normalize_path(path)
        stmts.extend(db.upsert_statements(meta, now))
        if prev:
            grand["updated"] += 1
        else:
            grand["added"] += 1
        with _STATE_LOCK:
            STATE["done"] += 1
            STATE["current"] = f"{lib['name']} · {Path(path).name}"
        if len(stmts) >= 400:
            flush()
            time.sleep(0)      # 让 UI 线程与 HTTP 线程有机会拿到锁
    flush()


def _reconcile(lib: Dict[str, Any], files: List[str], grand: Dict[str, int]) -> int:
    # 先把「上次被标失效、这次又被走查到」的行整体复活，再按本轮文件清单重新标记。
    # 顺序不能反：mark_missing_not_in 只负责把不在清单里的打成 1，它不负责把回来的打成 0；
    # 而 _ingest 对 size+mtime 没变的文件是跳过的（增量扫描的全部意义），所以复活这件事
    # 只能在 reconcile 这一段做。少了这一步，「取消排除后重扫」「U 盘重新插上」这两种
    # 情况都会让曲目永远停在失效状态 —— 闸门里的 scan-revive 就是钉这个的。
    was = {int(r["id"]) for r in db.query(
        "SELECT id FROM tracks WHERE missing = 1 AND library_id = ?", (lib["id"],))}
    if was:
        db.write("UPDATE tracks SET missing = 0 WHERE missing = 1 AND library_id = ?", (lib["id"],))
    gone = db.mark_missing_not_in([normalize_path(f) for f in files], lib["id"])
    back = len(was - set(gone))        # 真回来的 = 之前失效的 ∖ 这轮依然不在清单里的
    if back:
        _set(current=f"找回 {back} 条之前判定失效的曲目")
    grand["removed"] += len(gone)
    db.write("UPDATE libraries SET last_scan = ?, track_count = ? WHERE id = ?", (
        time.time(),
        db.track_count(["library_id = ?"], [lib["id"]]),
        lib["id"],
    ))
    return len(gone)


# ------------------------------------------------------------------ async

_scan_thread: Optional[threading.Thread] = None


def scan_async(library_id: Optional[int] = None, include_unsupported: bool = False,
               force: bool = False) -> Dict[str, Any]:
    global _scan_thread
    with _STATE_LOCK:
        if STATE["running"]:
            return {"ok": False, "busy": True, **state_snapshot()}
    if not _SCAN_LOCK.acquire(blocking=False):
        # 上面那道 running 检查在空档里会放行第二个请求，这里才是真正拦住它的门。
        return {"ok": False, "busy": True, **state_snapshot()}

    def run() -> None:
        try:
            scan_library(library_id, include_unsupported, force)
        finally:
            _SCAN_LOCK.release()

    _scan_thread = threading.Thread(target=run, daemon=True, name="salt-scan")
    _scan_thread.start()
    return {"ok": True, "started": True}


def quick_stats() -> Dict[str, Any]:
    return db.stats()

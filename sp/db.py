"""SQLite layer: tracks, libraries, playlists, favorites, play history."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from .config import db_path, normalize_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    name        TEXT,
    added_at    REAL NOT NULL,
    last_scan   REAL,
    track_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    album_artist TEXT,
    genre        TEXT,
    year         INTEGER,
    track_no     INTEGER,
    disc_no      INTEGER,
    duration     REAL NOT NULL DEFAULT 0,
    bitrate      INTEGER NOT NULL DEFAULT 0,
    sample_rate  INTEGER NOT NULL DEFAULT 0,
    bit_depth    INTEGER NOT NULL DEFAULT 0,
    channels     INTEGER NOT NULL DEFAULT 0,
    fmt          TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL NOT NULL DEFAULT 0,
    ctime        REAL NOT NULL DEFAULT 0,
    folder       TEXT,
    filename     TEXT,
    lyric_path   TEXT,
    embedded_lyrics TEXT,
    playable     INTEGER NOT NULL DEFAULT 1,
    has_cover    INTEGER NOT NULL DEFAULT 0,
    cover_path   TEXT,
    library_id   INTEGER REFERENCES libraries(id) ON DELETE SET NULL,
    added_at     REAL NOT NULL,
    updated_at   REAL NOT NULL,
    missing      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(lower(artist));
CREATE INDEX IF NOT EXISTS idx_tracks_album  ON tracks(lower(album), lower(album_artist));
CREATE INDEX IF NOT EXISTS idx_tracks_folder ON tracks(folder);
CREATE INDEX IF NOT EXISTS idx_tracks_lib    ON tracks(library_id, missing);
-- 下面四条是为「列表默认排序 / 计数 / 常用切换排序」补的（实测 20k 首，压测见
-- tools/probe_scale.py）：没有它们时 SELECT 要先全表扫再 USE TEMP B-TREE 排序，
-- 而排序键里带着 embedded_lyrics 这种大字段，深分页一页就要 0.5s。
-- 索引只含被筛/被排的小列，配合窄投影后同一页掉到几十毫秒。
CREATE INDEX IF NOT EXISTS idx_tracks_default_order ON tracks(missing, playable, artist, album, disc_no, track_no);
CREATE INDEX IF NOT EXISTS idx_tracks_count ON tracks(missing, playable);
CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_tracks_added ON tracks(added_at DESC);
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id INTEGER NOT NULL,
    track_id    INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    added_at    REAL NOT NULL,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_pli_pos ON playlist_items(playlist_id, position);
CREATE TABLE IF NOT EXISTS favorites (
    track_id INTEGER PRIMARY KEY,
    added_at REAL NOT NULL
);
-- 收藏列表按 added_at 倒序翻（原来是 SCAN favorites + TEMP B-TREE 排序）
CREATE INDEX IF NOT EXISTS idx_fav_added ON favorites(added_at DESC, track_id);
CREATE TABLE IF NOT EXISTS plays (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id  INTEGER NOT NULL,
    played_at REAL NOT NULL,
    ms        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plays_track ON plays(track_id, played_at DESC);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# 早期版本建库时会塞一条同名歌单，而侧栏顶部本来就有 ♥「我喜欢的音乐」视图，两个同名
# 不同物的入口让人分不清点的是哪个。常量留着只为清理老库，不再用来造新行。
LEGACY_FAV_PLAYLIST_NAME = "我喜欢的音乐"

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None
# 分页每翻一页都要 total，而 total 是 COUNT(*) 全表扫（实测 20k 首 30~50 ms）。
# 翻页途中库内容不会变，所以按「筛选条件 + 写版本号」缓存：任何 write/many/script
# 都会把 _WRITE_GEN 抬起来，缓存随即失效，不会把旧数字端给前端。
_COUNT_CACHE: Dict[Any, Any] = {}
_WRITE_GEN = 0


def conn() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = sqlite3.connect(str(db_path()), check_same_thread=False, timeout=30.0)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA synchronous=NORMAL")
            _CONN.execute("PRAGMA foreign_keys=ON")
            _CONN.executescript(SCHEMA)
            _CONN.commit()
            _seed()
        return _CONN


def _seed() -> None:
    c = _CONN
    ensure_columns(c)
    # 老库里那条自动造的「我喜欢的音乐」歌单：还是空的就顺手删掉（它跟侧栏的 ♥ 视图重名，
    # 是历史遗留而不是用户建的东西）。一旦用户往里加过歌就不碰 —— 那时它是用户的数据了，
    # 删它等于丢歌单。playlist_items 是这条判断的唯一依据。
    c.execute(
        "DELETE FROM playlists WHERE name = ?"
        "  AND id NOT IN (SELECT playlist_id FROM playlist_items)",
        (LEGACY_FAV_PLAYLIST_NAME,),
    )
    c.commit()


# Columns added after the first release: older databases get them ALTERed in.
_MIGRATIONS = {
    "tracks": {
        "embedded_lyrics": "TEXT",
        "playable": "INTEGER NOT NULL DEFAULT 1",
        "isrc": "TEXT",
        # 源位深（0＝没量到）。独占档的 bit-perfect 判据要它：把「设备只给 16 位」和
        # 「源本来就是 16 位」混成一锅，界面就只能在「全都说不是 bit-perfect」和
        # 「全都说是」之间选一个，两个都是假话。老库靠起播时的 lazy probe 补。
        "bit_depth": "INTEGER NOT NULL DEFAULT 0",
    },
    "playlists": {"pinned": "INTEGER NOT NULL DEFAULT 0", "description": "TEXT"},
}


def ensure_columns(c: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, decl in cols.items():
            if col not in have:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:
                    pass
    c.commit()


def query(sql: str, args: Iterable[Any] = ()) -> List[sqlite3.Row]:
    with _LOCK:
        return conn().execute(sql, tuple(args)).fetchall()


def query_one(sql: str, args: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    with _LOCK:
        return conn().execute(sql, tuple(args)).fetchone()


def write(sql: str, args: Iterable[Any] = ()) -> int:
    with _LOCK:
        global _WRITE_GEN
        c = conn()
        cur = c.execute(sql, tuple(args))
        c.commit()
        _WRITE_GEN += 1
        _COUNT_CACHE.clear()
        return cur.lastrowid


def many(sql: str, seq: Iterable[Iterable[Any]]) -> int:
    with _LOCK:
        global _WRITE_GEN
        c = conn()
        cur = c.executemany(sql, [tuple(s) for s in seq])
        c.commit()
        _WRITE_GEN += 1
        _COUNT_CACHE.clear()
        return cur.rowcount


def script(sql: str) -> None:
    with _LOCK:
        global _WRITE_GEN
        c = conn()
        c.executescript(sql)
        c.commit()
        _WRITE_GEN += 1
        _COUNT_CACHE.clear()


def batch(stmts: List[tuple]) -> int:
    """一串 (sql, args) 放在同一个事务里提交。

    扫描器原来每首一首歌调 write()，而 write() 每次都 commit —— 20k 首就是 20k 次
    WAL 落盘（Windows 上每次 commit 都是真系统调用）。改成一批一次提交之后，导入
    整个文件夹的时间从「按首数线性放大的 fsync」变成一次。中途出错整批回滚，
    不会留下「一半新库」。
    """
    if not stmts:
        return 0
    with _LOCK:
        global _WRITE_GEN
        c = conn()
        try:
            for sql, args in stmts:
                c.execute(sql, tuple(args))
            c.commit()
        except Exception:
            c.rollback()
            raise
        _WRITE_GEN += 1
        _COUNT_CACHE.clear()
    return len(stmts)


# ---------------------------------------------------------------- libraries

def add_library(path: str, name: str = "") -> int:
    np = normalize_path(path)
    now = time.time()
    row = query_one("SELECT id FROM libraries WHERE path = ?", (np,))
    if row:
        return int(row["id"])
    return write(
        "INSERT INTO libraries(path, name, added_at) VALUES(?,?,?)",
        (np, name or np.rstrip("\\/").split(os.sep)[-1] or np, now),
    )


# ------------------------------------------------------------------ 目录黑名单
# 存在 meta 里而不是新开一张表：它就是一串路径，永远整体读写、从不按单条查询，
# 建表反而要多一套 JOIN。规范化用 normalize_path（和 tracks.path 同一个键空间），
# 所以比较时只比大小写，不再碰文件系统。
BLACKLIST_KEY = "dir_blacklist"


def blacklist_dirs() -> List[str]:
    raw = get_meta(BLACKLIST_KEY, [])
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


def _dirkey(p: str) -> str:
    return normalize_path(p).lower().rstrip("\\/")


def covers(outer: str, inner: str) -> bool:
    """outer 本身或它的子目录 = inner。两端都要先过 _dirkey，否则大小写和尾斜杠会漏判。"""
    o, i = _dirkey(outer), _dirkey(inner)
    return bool(o) and (i == o or i.startswith(o + "\\"))


def add_blacklist(path: str) -> Dict[str, Any]:
    """加入排除目录。重复与「已被更上层覆盖」直接返回，反向冗余（新目录正好罩住
    原来几条更深的）则顺手收掉：排除 D:\\Music 之后 D:\\Music\\Rock 那条就该没了。"""
    np = normalize_path(path)
    if not np or not os.path.isdir(np):
        return {"ok": False, "error": "文件夹不存在", "items": blacklist_dirs()}
    cur = [normalize_path(p) for p in blacklist_dirs()]
    if any(covers(c, np) for c in cur):
        return {"ok": True, "items": cur, "already": True}
    kept = [c for c in cur if not covers(np, c)]
    kept.append(np)
    set_meta(BLACKLIST_KEY, kept)
    return {"ok": True, "items": kept}


def remove_blacklist(path: str) -> Dict[str, Any]:
    k = _dirkey(path)
    cur = [c for c in blacklist_dirs() if _dirkey(c) != k]
    set_meta(BLACKLIST_KEY, cur)
    return {"ok": True, "items": cur}


def list_libraries() -> List[Dict[str, Any]]:
    """一行一个库，外加「此刻库里还有几首」（track_count 那列是扫描时写的快照，不能用）。

    这版曾经是两条语句：SELECT * FROM libraries + library_counts() 的一条
    `GROUP BY library_id`。20000 首夹具上实测反而更慢（bootstrap 56.1ms → 69.2ms）：
    GROUP BY 要把 tracks 的 2 万行全数过一遍，而关联子查询走 idx_tracks_lib，每个库只
    数自己那一截；库数量级是个位数，所以「一条大扫描」换成「几段索引跳」反而亏。
    结论：退回单条语句。想再优化得给 (library_id, missing) 建覆盖索引，别改这里的形状。
    """
    rows = query(
        "SELECT l.*, (SELECT COUNT(*) FROM tracks t WHERE t.library_id = l.id AND t.missing = 0) AS cnt"
        " FROM libraries l ORDER BY l.added_at")
    return [dict(r, track_count=int(r["cnt"])) for r in rows]


def remove_library(library_id: int, keep_tracks: bool = False) -> None:
    if keep_tracks:
        write("UPDATE tracks SET library_id = NULL WHERE library_id = ?", (library_id,))
    else:
        ids = [r["id"] for r in query("SELECT id FROM tracks WHERE library_id = ?", (library_id,))]
        for tid in ids:
            delete_track_refs(tid)
        write("DELETE FROM tracks WHERE library_id = ?", (library_id,))
    write("DELETE FROM libraries WHERE id = ?", (library_id,))


def library_for(path: str) -> Optional[int]:
    """Innermost registered library that contains *path*."""
    np = normalize_path(path)
    best: Optional[tuple[int, int]] = None
    for r in query("SELECT id, path FROM libraries"):
        lp = r["path"]
        if np == lp or np.upper().startswith(lp.upper() + os.sep) or np.upper().startswith(lp.upper() + "/"):
            if best is None or len(lp) > best[1]:
                best = (int(r["id"]), len(lp))
    return best[0] if best else None


# ------------------------------------------------------------------- tracks

TRACK_COLUMNS = (
    "id, path, title, artist, album, album_artist, genre, year, track_no, disc_no, duration,"
    " bitrate, sample_rate, bit_depth, channels, fmt, size, mtime, folder, filename, lyric_path,"
    " embedded_lyrics, playable, has_cover, cover_path, library_id, added_at, missing"
)


def track_cols(prefix: str) -> str:
    """TRACK_COLUMNS with every name qualified by *prefix*.

    Needed by joins: ``playlist_items`` also has ``added_at``, so a bare column
    list raises "ambiguous column name" there.
    """
    return ", ".join(prefix + c.strip() for c in TRACK_COLUMNS.split(","))


# 列表投影：去掉 path / lyric_path / cover_path / embedded_lyrics 四列。
# 实测 20k 首（其中 25% 带内嵌歌词）：歌单 2000 行的响应 2865 KB，其中歌词正文占
# 一大半，而前端一行列表只需要「标题+艺人+专辑+时长+有没有封面」。
# 需要全文的场合（详情面板、复制路径、在文件夹中显示、取歌词）都有各自接口，
# 它们按 id 取单条（/api/track/<id>，实测 2.7 ms），代价比整表扛着全文宜。
LIST_COLUMNS = (
    "id, title, artist, album, album_artist, genre, year, track_no, disc_no, duration,"
    " bitrate, sample_rate, channels, fmt, size, mtime, folder, filename,"
    " playable, has_cover, library_id, added_at, missing,"
    # 有没有词：前端要在行上标「词」，验证脚本要挑「有封面又有词」的样本，
    # 但谁也不需要歌词正文本身 —— 正文在 /api/lyrics 里按需取。
    " ((lyric_path IS NOT NULL AND lyric_path <> '')"
    "  OR (embedded_lyrics IS NOT NULL AND embedded_lyrics <> '')) AS has_lyric"
)


def list_cols(prefix: str = "") -> str:
    out = []
    for c in LIST_COLUMNS.split(","):
        c = c.strip()
        # 带括号的那几列（has_lyric）是表达式，本身已经写全了；加前缀会拼出
        # "t.((lyric_path ...))" 这种废语法。里面的 lyric_path / embedded_lyrics 只存在于
        # tracks（playlist_items 没有同名列），所以在 join 里不加前缀也不歧义。
        out.append((prefix + c) if (prefix and "(" not in c) else c)
    return ", ".join(out)


def upsert_statements(data: Dict[str, Any], now: Optional[float] = None) -> List[tuple]:
    """把一首歌变成待执行的 (sql, args) 列表，交给调用方攒批。

    用 INSERT ... ON CONFLICT(path) 而不是「先 SELECT 再决定 INSERT/UPDATE」：
    path 上有 UNIQUE，冲突即更新，一条语句顶原来的一条查加一条写。added_at 只在
    插入时写，更新时故意不碰，所以「入库时间」不会被重新扫描刷掉。
    """
    ts = now if now is not None else time.time()
    path = normalize_path(data["path"])
    d = {**data, "path": path, "updated_at": ts}
    cols = [k for k in d if k in TRACK_COLUMNS or k in ("ctime", "mtime")]
    insert_cols = cols + ["added_at", "updated_at", "missing"]
    sets = ", ".join(f"{k} = excluded.{k}" for k in cols)
    sql = (
        f"INSERT INTO tracks({', '.join(insert_cols)}) VALUES({', '.join('?' * len(insert_cols))})"
        f" ON CONFLICT(path) DO UPDATE SET {sets}, updated_at = excluded.updated_at, missing = 0"
    )
    vals = [d.get(k) for k in cols] + [ts, ts, 0]
    return [(sql, vals)]


def upsert_track(data: Dict[str, Any]) -> int:
    """单首歌的立即写入（一次 commit）。扫描器请走 upsert_statements + batch。"""
    stmts = upsert_statements(data)
    batch(stmts)
    r = query_one("SELECT id FROM tracks WHERE path = ?", (normalize_path(data["path"]),))
    return int(r["id"]) if r else 0


def delete_track_refs(track_id: int) -> None:
    script(
        f"DELETE FROM playlist_items WHERE track_id = {int(track_id)};"
        f"DELETE FROM favorites WHERE track_id = {int(track_id)};"
        f"DELETE FROM plays WHERE track_id = {int(track_id)};"
    )


def mark_missing_not_in(paths: Iterable[str], library_id: Optional[int] = None) -> List[int]:
    keep = {normalize_path(p) for p in paths}
    sql = "SELECT id, path FROM tracks" + (" WHERE library_id = ?" if library_id else "")
    rows = query(sql, (library_id,) if library_id else ())
    gone = [int(r["id"]) for r in rows if r["path"] not in keep]
    if gone:
        # 原来一首一首 write()，删一千首就是一千次 commit；现在一条 UPDATE ... IN 解决。
        ph = ",".join("?" * len(gone))
        write(f"UPDATE tracks SET missing = 1 WHERE id IN ({ph})", gone)
    return gone


def purge_missing() -> int:
    rows = query("SELECT id FROM tracks WHERE missing = 1")
    for r in rows:
        delete_track_refs(int(r["id"]))
    write("DELETE FROM tracks WHERE missing = 1")
    return len(rows)


def get_track(track_id: int) -> Optional[Dict[str, Any]]:
    r = query_one(f"SELECT {TRACK_COLUMNS}, ctime FROM tracks WHERE id = ?", (track_id,))
    return _decorate(dict(r)) if r else None


def _decorate(d: Dict[str, Any]) -> Dict[str, Any]:
    r = query_one("SELECT 1 FROM favorites WHERE track_id = ?", (d["id"],))
    d["favorite"] = bool(r)
    pr = query_one(
        "SELECT COUNT(*) AS n, MAX(played_at) AS last FROM plays WHERE track_id = ?", (d["id"],)
    )
    d["plays"] = int(pr["n"]) if pr else 0
    d["last_played"] = float(pr["last"]) if pr and pr["last"] else None
    return d


def cover_info(track_id: int) -> Optional[Dict[str, Any]]:
    """封面接口专用：一条 SQL 同时拿到「图在哪」和「没图时画字块要用的文字」。

    以前 _cover 调 get_track()，那是 3 条 SQL；一屏 30 张封面就是 90 条，
    而且它们要和音频流请求抢同一把全局锁。
    """
    r = query_one(
        "SELECT cover_path, has_cover, mtime, title, artist, album FROM tracks WHERE id = ?",
        (int(track_id),))
    return dict(r) if r else None


def favorite_set() -> set:
    """全部收藏的 id 集合。列表行要标 ♥ 时用这一条 SQL，不要逐行 SELECT 1。"""
    return {int(r["track_id"]) for r in query("SELECT track_id FROM favorites")}


def filter_tracks(
    where: List[str],
    args: List[Any],
    limit: int = 0,
    offset: int = 0,
    order: str = "artist, album, disc_no, track_no, id",
    cols: str = TRACK_COLUMNS,
) -> List[Dict[str, Any]]:
    w = " AND ".join(where + ["missing = 0"])
    sql = f"SELECT {cols} FROM tracks WHERE {w} ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = query(sql, args)
    fav = favorite_set()
    return [dict(r, favorite=r["id"] in fav) for r in rows]


def tracks_by_ids(ids: List[int], cols: str = TRACK_COLUMNS) -> List[Dict[str, Any]]:
    """按给定顺序取多条：一条取行 + 一条收藏 + 一条播放统计，共 3 条 SQL。

    以前这里是对每个 id 调 get_track()，而 get_track 里的 _decorate 每条又要发两条，
    于是 500 首收藏 = 1501 条 SQL（实测，见 tools/scale_before.log）。收藏/历史/队列
    都是「整屏都是行」的场景，N+1 在这种规模下就是主要耗时。
    """
    ids = [int(i) for i in ids]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    by = {int(r["id"]): dict(r) for r in
          query(f"SELECT {cols} FROM tracks WHERE id IN ({ph})", ids)}
    fav = {int(r["track_id"]) for r in
           query(f"SELECT track_id FROM favorites WHERE track_id IN ({ph})", ids)}
    plays: Dict[int, Any] = {int(r["track_id"]): r for r in query(
        f"SELECT track_id, COUNT(*) AS n, MAX(played_at) AS last FROM plays "
        f"WHERE track_id IN ({ph}) GROUP BY track_id", ids)}
    out = []
    for tid in ids:                     # 保持调用方给的顺序（历史/队列/拖拽都靠它）
        d = by.get(tid)
        if not d or d.get("missing"):
            continue
        d["favorite"] = tid in fav
        pr = plays.get(tid)
        d["plays"] = int(pr["n"]) if pr else 0
        d["last_played"] = float(pr["last"]) if pr and pr["last"] else None
        out.append(d)
    return out


def scalar_map(ids: List[int], col: str) -> Dict[int, Any]:
    """一批 track id 的同一列（现在只有专辑行用来补 genre），一条 SQL 拿完。"""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    assert re.fullmatch(r"[a-z_]+", col), f"列名只允许内部字面量，收到 {col!r}"
    ph = ",".join("?" * len(ids))
    return {int(r["id"]): r["v"] for r in
            query(f"SELECT id, {col} AS v FROM tracks WHERE id IN ({ph})", ids)}


def write_gen() -> int:
    """当前写版本号：任何 write/many/script/batch 都会让它 +1。

    给「按数据版本缓存」的调用方用（api 里的聚合视图缓存）。别拿它做别的判断 ——
    它只回答「这段时间里库有没有被写过」，不回答写了什么。
    """
    with _LOCK:
        return _WRITE_GEN


def track_count(where: List[str], args: List[Any]) -> int:
    """筛选条件下的总数。同一代（期间没有写库）里按条件缓存，翻页不再重复全表 COUNT。"""
    w = " AND ".join(where + ["missing = 0"])
    key = (w, tuple(args))
    hit = _COUNT_CACHE.get(key)
    if hit and hit[0] == _WRITE_GEN:
        return int(hit[1])
    r = query_one(f"SELECT COUNT(*) AS n FROM tracks WHERE {w}", args)
    n = int(r["n"]) if r else 0
    if len(_COUNT_CACHE) > 256:          # 搜索词是用户敲出来的，会无限造新 key
        _COUNT_CACHE.clear()
    _COUNT_CACHE[key] = (_WRITE_GEN, n)
    return n


# ---------------------------------------------------------------- playlists

def create_playlist(name: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("playlist name required")
    if query_one("SELECT id FROM playlists WHERE name = ?", (name,)):
        raise ValueError("duplicate")
    now = time.time()
    row = query_one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM playlists")
    mx = int(row["m"]) if row else 0
    return write(
        "INSERT INTO playlists(name, sort_order, created_at, updated_at) VALUES(?,?,?,?)",
        (name, mx + 1, now, now),
    )


def rename_playlist(pid: int, name: str) -> None:
    write("UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?", (name.strip(), time.time(), pid))


def delete_playlist(pid: int) -> None:
    write("DELETE FROM playlist_items WHERE playlist_id = ?", (pid,))
    write("DELETE FROM playlists WHERE id = ?", (pid,))


def restore_playlist(snap: Dict[str, Any]) -> int:
    """把删除前的快照写回库里 —— 给前端的「撤销删除」用。

    snap = {name, id?, sort_order?, pinned?, created_at?, track_ids:[...]}

    三处退让都是有原因的：
    * 老 id 还空着就复用，撤销时侧栏的 #/playlist/<id> 可能还指着它，换个号链接就断了；
      已被别的歌单占了就作罢，绝不能把别人的行覆盖掉。
    * 名字撞了（用户在气泡消失前又建了同名歌单）时给恢复出来的那条加后缀，
      而不是覆盖用户刚建的 —— 那是新数据，比快照重要。
    * 曲目只认库里还在的（missing = 0），删除期间扫描器可能已经把某首移走了。
    """
    name = str(snap.get("name") or "").strip() or "已恢复的歌单"
    now = time.time()
    raw_id = snap.get("id")
    pid = int(raw_id) if str(raw_id).isdigit() else None
    if pid is not None and query_one("SELECT id FROM playlists WHERE id = ?", (pid,)):
        pid = None
    if query_one("SELECT id FROM playlists WHERE name = ?", (name,)):
        base, i = name, 2
        while query_one("SELECT id FROM playlists WHERE name = ?", (f"{base}（恢复 {i}）",)):
            i += 1
        name, pid = f"{base}（恢复 {i}）", None
    new_id = write(
        "INSERT INTO playlists(id, name, sort_order, created_at, updated_at, pinned) VALUES(?,?,?,?,?,?)",
        (pid, name, int(snap.get("sort_order") or 0),
         float(snap.get("created_at") or now), now, 1 if snap.get("pinned") else 0),
    )
    ids = [int(x) for x in (snap.get("track_ids") or []) if str(x).isdigit()]
    if ids:
        ph = ",".join("?" * len(ids))
        alive = {int(r["id"]) for r in
                 query(f"SELECT id FROM tracks WHERE id IN ({ph}) AND missing = 0", ids)}
        batch([("INSERT OR IGNORE INTO playlist_items(playlist_id, track_id, position, added_at)"
                " VALUES(?,?,?,?)", (new_id, tid, pos, now))
               for pos, tid in enumerate(ids) if tid in alive])
    return int(new_id)


def set_playlist_pinned(pid: int, on: bool) -> None:
    """置顶开关。list_playlists 的 ORDER BY 是 pinned DESC, sort_order, id，
    所以置顶只影响它和「非置顶歌单」之间的先后，置顶组内部仍按用户拖出来的顺序。"""
    write("UPDATE playlists SET pinned = ? WHERE id = ?", (1 if on else 0, int(pid)))


def reorder_playlists(ids: List[int]) -> int:
    """按给定顺序重写 sort_order（list_playlists 就是按它排的）。

    只认库里真实存在的那些 id：前端拿的是拖动前的快照，期间别处（另一个窗口、
    刚建好的歌单）可能已经变了，把不存在的 id 写进去等于凭空造顺序。
    updated_at 故意不碰 —— 它表示「歌单内容动过」，重排侧栏不算。
    """
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    known = {int(r["id"]) for r in query(f"SELECT id FROM playlists WHERE id IN ({ph})", ids)}
    rows = [(i, pid) for i, pid in enumerate(ids) if pid in known]
    many("UPDATE playlists SET sort_order = ? WHERE id = ?", rows)
    return len(rows)


def list_playlists() -> List[Dict[str, Any]]:
    rows = query(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM playlist_items i JOIN tracks t ON t.id = i.track_id
                        WHERE i.playlist_id = p.id AND t.missing = 0) AS cnt,
                  (SELECT COALESCE(SUM(t.duration),0) FROM playlist_items i JOIN tracks t ON t.id = i.track_id
                        WHERE i.playlist_id = p.id AND t.missing = 0) AS dur,
                  (SELECT i.track_id FROM playlist_items i JOIN tracks t ON t.id = i.track_id
                        WHERE i.playlist_id = p.id AND t.has_cover = 1 AND t.missing = 0
                   ORDER BY i.position LIMIT 1) AS cover_track
           FROM playlists p ORDER BY p.pinned DESC, p.sort_order, p.id"""
    )
    out = []
    for r in rows:
        d = dict(r)
        d["track_count"] = d.pop("cnt")
        d["duration"] = float(d.pop("dur"))
        out.append(d)
    return out


def playlist_tracks(pid: int, limit: int = 0, offset: int = 0,
                    cols: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = (
        f"SELECT {(cols or track_cols('t.'))}, i.position AS pos, i.added_at AS item_added_at "
        f"FROM playlist_items i "
        f"JOIN tracks t ON t.id = i.track_id WHERE i.playlist_id = ? AND t.missing = 0 "
        f"ORDER BY i.position"
    )
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = query(sql, (pid,))
    fav = favorite_set()
    return [{**dict(r), "favorite": r["id"] in fav} for r in rows]


def playlist_count(pid: int) -> int:
    """歌单里「还能播」的行数。

    分页的前端必须知道总数，否则它没法判断该不该继续翻。口径要和 playlist_tracks 的
    JOIN 完全一致（t.missing = 0）：用 COUNT(*) FROM playlist_items 会把失效行也数进去，
    于是列表永远翻不到底 —— 差的那几首恰好是用户删掉文件后留下的行。
    """
    row = query_one(
        "SELECT COUNT(*) AS n FROM playlist_items i "
        "JOIN tracks t ON t.id = i.track_id WHERE i.playlist_id = ? AND t.missing = 0",
        (int(pid),),
    )
    return int(row["n"]) if row else 0


def add_to_playlist(pid: int, track_ids: List[int], position: int = -1) -> int:
    """把若干首加进歌单。

    position < 0（默认）是「追加到末尾」，只写新行、不碰已有的 —— 往 2000 首的歌单里
    加一首也不该重排整张表。position >= 0 是「插到第 position 格」，那时必须整体重排：
    不重排的话插进去的值会和已有的行撞同一个 position，顺序就从列表里乱了。
    撤销「从歌单移除一首」走的就是这条插队路径（要放回原来那一格）。
    """
    now = time.time()
    pid = int(pid)
    wanted = [int(x) for x in (track_ids or [])]
    existing = {int(r["track_id"]) for r in playlist_items(pid)}
    ids: List[int] = []
    for tid in wanted:
        if tid in existing or tid in ids:
            continue
        ids.append(tid)
    if not ids:
        return 0
    if position is None or int(position) < 0:
        base = query_one("SELECT COALESCE(MAX(position), -1) AS m FROM playlist_items WHERE playlist_id = ?",
                         (pid,))["m"]
        batch([("INSERT INTO playlist_items(playlist_id, track_id, position, added_at) VALUES(?,?,?,?)",
                (pid, tid, int(base) + 1 + i, now)) for i, tid in enumerate(ids)]
              + [("UPDATE playlists SET updated_at = ? WHERE id = ?", (now, pid))])
        return len(ids)
    cur = [int(r["track_id"]) for r in playlist_items(pid)]
    at = max(0, min(int(position), len(cur)))
    merged = cur[:at] + ids + cur[at:]
    fresh = set(ids)
    batch([("INSERT INTO playlist_items(playlist_id, track_id, position, added_at) VALUES(?,?,?,?)",
            (pid, tid, i, now)) if tid in fresh else
           ("UPDATE playlist_items SET position = ? WHERE playlist_id = ? AND track_id = ?", (i, pid, tid))
           for i, tid in enumerate(merged)]
          + [("UPDATE playlists SET updated_at = ? WHERE id = ?", (now, pid))])
    return len(ids)


def remove_from_playlist(pid: int, track_id: int) -> int:
    """从歌单删掉一首，并把剩下的重排成连续的 0..n-1。

    重排是「撤销移除」的前提：不重排就留着洞，position 和「列表里的第几首」不再是
    一回事，按原下标插回去会落到别的位置上。编号规则和 move_in_playlist 一致。
    """
    pid, track_id = int(pid), int(track_id)
    all_ids = [int(r["track_id"]) for r in playlist_items(pid)]
    if track_id not in all_ids:
        return 0
    rest = [t for t in all_ids if t != track_id]
    stmts = [("DELETE FROM playlist_items WHERE playlist_id = ? AND track_id = ?", (pid, track_id))]
    stmts += [("UPDATE playlist_items SET position = ? WHERE playlist_id = ? AND track_id = ?", (i, pid, tid))
              for i, tid in enumerate(rest)]
    stmts.append(("UPDATE playlists SET updated_at = ? WHERE id = ?", (time.time(), pid)))
    batch(stmts)
    return 1


def move_in_playlist(pid: int, track_id: int, new_pos: int) -> None:
    ids = [r["track_id"] for r in playlist_items(pid)]
    if track_id not in ids:
        return
    ids.remove(track_id)
    new_pos = max(0, min(len(ids), int(new_pos)))
    ids.insert(new_pos, track_id)
    with _LOCK:
        c = conn()
        for i, tid in enumerate(ids):
            c.execute(
                "UPDATE playlist_items SET position = ? WHERE playlist_id = ? AND track_id = ?",
                (i, pid, tid),
            )
        c.commit()


def playlist_items(pid: int) -> List[sqlite3.Row]:
    return query(
        "SELECT track_id, position FROM playlist_items WHERE playlist_id = ? ORDER BY position", (pid,)
    )


def playlists_of(track_id: int) -> List[int]:
    return [int(r["playlist_id"]) for r in query("SELECT playlist_id FROM playlist_items WHERE track_id = ?", (track_id,))]


# --------------------------------------------------------------- favorites

def toggle_favorite(track_id: int) -> bool:
    if query_one("SELECT 1 FROM favorites WHERE track_id = ?", (track_id,)):
        write("DELETE FROM favorites WHERE track_id = ?", (track_id,))
        return False
    write("INSERT INTO favorites(track_id, added_at) VALUES(?,?)", (track_id, time.time()))
    return True


def favorite_ids() -> List[int]:
    return [int(r["track_id"]) for r in query("SELECT track_id FROM favorites ORDER BY added_at DESC")]


# -------------------------------------------------------------------- plays

def record_play(track_id: int, ms: int = 0) -> None:
    write("INSERT INTO plays(track_id, played_at, ms) VALUES(?,?,?)", (track_id, time.time(), int(ms)))


def recent_ids(limit: int = 50) -> List[int]:
    rows = query(
        "SELECT track_id FROM plays GROUP BY track_id ORDER BY MAX(played_at) DESC LIMIT ?", (int(limit),)
    )
    return [int(r["track_id"]) for r in rows]


def favorite_rows(limit: int = 0, offset: int = 0, cols: Optional[str] = None) -> List[Dict[str, Any]]:
    """收藏列表（带 total 由调用方另取）。ORDER BY added_at 走 idx_fav_added。"""
    sql = "SELECT track_id FROM favorites ORDER BY added_at DESC"
    args: List[Any] = []
    if limit:
        sql += " LIMIT ? OFFSET ?"
        args = [int(limit), int(offset)]
    return tracks_by_ids([int(r["track_id"]) for r in query(sql, args)], cols or TRACK_COLUMNS)


def recent_rows(limit: int = 60, cols: Optional[str] = None) -> List[Dict[str, Any]]:
    return tracks_by_ids(recent_ids(limit), cols or TRACK_COLUMNS)


def stats() -> Dict[str, Any]:
    """侧栏/关于页的四个总数。bootstrap 每次都要，而它是 COUNT(DISTINCT lower(...))
    全表扫（实测 20k 首 ≈25 ms），所以按写版本号缓存；写一次库就重算。"""
    hit = _COUNT_CACHE.get(("__stats__",))
    if hit and hit[0] == _WRITE_GEN:
        return dict(hit[1])
    # 总数用与聚合页相同的组身份：专辑 =（专辑名, 组艺人），组艺人空则退回收音
    # 艺术家；空专辑名不属于任何专辑。只按 lower(album) 去重会把不同艺人的同名专辑
    # 并成一张（首页 371 张，专辑页 443 张），也会把空专辑名算成一张。
    r = query_one(
        """SELECT COUNT(*) AS tracks, COALESCE(SUM(duration),0) AS secs,
                  COUNT(DISTINCT lower(COALESCE(NULLIF(album_artist, ''), artist))) AS artists,
                  COUNT(DISTINCT CASE WHEN album IS NOT NULL AND album <> ''
                       THEN lower(album) || char(31) ||
                            lower(COALESCE(NULLIF(album_artist, ''), artist)) END) AS albums
           FROM tracks WHERE missing = 0"""
    )
    f = query_one("SELECT COUNT(*) AS n FROM favorites")
    p = query_one("SELECT COUNT(*) AS n FROM plays")
    out = {
        "tracks": int(r["tracks"]),
        "duration": float(r["secs"]),
        "artists": int(r["artists"]),
        "albums": int(r["albums"]),
        "favorites": int(f["n"]),
        "plays": int(p["n"]),
    }
    # 「读缓存」这一半以前是空的：写了 if 命中就返回，却从没往 _COUNT_CACHE 存过，
    # 于是 bootstrap 每次 5 发中位仍是 28 ms（tools/scale_after.log）。缓存必须有去无回，
    # 下面 test_agg_cache.py 里的 stats-misses-twice 那条就是盯这个的。
    if len(_COUNT_CACHE) > 256:
        _COUNT_CACHE.clear()
    _COUNT_CACHE[("__stats__",)] = (_WRITE_GEN, out)
    return dict(out)


# 按后缀判定「无损」：比按码率靠谱。WAV/FLAC 的码率是算出来的（44.1k×2×16=1411200），
# 但一张低码率转制的 .wav 仍然会落进「≥700k」；反过来 ALAC 装在 .m4a 里、码率又可能
# 低于门槛。这里只声明能从后缀确证的那一半，剩下的交给码率分档去看。
LOSSLESS_EXTS = {"flac", "wav", "wave", "aiff", "aif", "ape", "tak", "wv", "dsf"}

# 码率分档（bps）。上界含本档，第一档专门收「标签里没写码率」的 0。
BITRATE_BANDS = ((0, "未知"), (128000, "≤128k"), (192000, "128–192k"),
                 (320000, "192–320k"), (700000, "320k–无损之间"), (10 ** 12, "≥700k"))


def quality() -> Dict[str, Any]:
    """曲库品质分析：按格式、按码率档、按采样率各聚一次合。

    三条语句都要全表扫（missing=0 上没有覆盖 fmt/bitrate 的索引），实测 8 首量不出来，
    20k 首夹具上估计与 stats 同量级（≈25 ms/条），所以调用方必须走 _agg 缓存，
    不要每次打开设置页就重跑。
    """
    fmt_rows = query(
        """SELECT COALESCE(NULLIF(lower(fmt),''),'') AS fmt, COUNT(*) AS tracks,
                  COALESCE(SUM(duration),0) AS secs, COALESCE(SUM(size),0) AS bytes,
                  COALESCE(AVG(NULLIF(bitrate,0)),0) AS avg_bitrate,
                  COALESCE(MAX(bitrate),0) AS max_bitrate,
                  SUM(CASE WHEN sample_rate >= 88200 THEN 1 ELSE 0 END) AS hires
           FROM tracks WHERE missing = 0 GROUP BY COALESCE(NULLIF(lower(fmt),''),'')
           ORDER BY tracks DESC""")
    per_fmt = []
    for r in fmt_rows:
        ext = r["fmt"] or ""
        per_fmt.append({
            "fmt": ext or "未知",
            "tracks": int(r["tracks"]), "duration": float(r["secs"]), "size": int(r["bytes"]),
            "avg_bitrate": int(r["avg_bitrate"]), "max_bitrate": int(r["max_bitrate"]),
            "hires": int(r["hires"] or 0),
            "lossless": ext in LOSSLESS_EXTS,
        })
    band_rows = query(
        """SELECT bitrate AS b, COUNT(*) AS n, COALESCE(SUM(duration),0) AS secs
           FROM tracks WHERE missing = 0 GROUP BY bitrate""")
    bands = {label: {"tracks": 0, "duration": 0.0} for _, label in BITRATE_BANDS}
    for r in band_rows:
        b = int(r["b"] or 0)
        label = next((lab for hi, lab in BITRATE_BANDS if b <= hi), "≥700k")
        bands[label]["tracks"] += int(r["n"])
        bands[label]["duration"] += float(r["secs"])
    sr_rows = query(
        """SELECT sample_rate AS sr, COUNT(*) AS n FROM tracks
           WHERE missing = 0 GROUP BY sample_rate ORDER BY n DESC""")
    sample_rates = [{"hz": int(r["sr"] or 0), "tracks": int(r["n"])} for r in sr_rows]
    tot = query_one(
        """SELECT COUNT(*) AS tracks, COALESCE(SUM(duration),0) AS secs, COALESCE(SUM(size),0) AS bytes,
                  COALESCE(AVG(NULLIF(bitrate,0)),0) AS avg_bitrate,
                  SUM(CASE WHEN playable = 0 THEN 1 ELSE 0 END) AS unplayable
           FROM tracks WHERE missing = 0""")
    ll = sum(int(r["tracks"]) for r in fmt_rows if (r["fmt"] or "") in LOSSLESS_EXTS)
    n = int(tot["tracks"] or 0)
    return {
        "total": {"tracks": n, "duration": float(tot["secs"] or 0), "size": int(tot["bytes"] or 0),
                  "avg_bitrate": int(tot["avg_bitrate"] or 0),
                  "unplayable": int(tot["unplayable"] or 0)},
        "formats": per_fmt,
        "bands": [{"label": lab, "tracks": bands[lab]["tracks"], "duration": bands[lab]["duration"],
                   "share": (bands[lab]["tracks"] / n) if n else 0.0}
                  for _, lab in BITRATE_BANDS],
        "sample_rates": sample_rates,
        "lossless": {"tracks": ll, "share": (ll / n) if n else 0.0},
    }


# --------------------------------------------------------------------- meta

def get_meta(key: str, default: Any = None) -> Any:
    r = query_one("SELECT value FROM meta WHERE key = ?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return r["value"]


def set_meta(key: str, value: Any) -> None:
    write(
        "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


import os  # noqa: E402  (used by normalize helpers above)

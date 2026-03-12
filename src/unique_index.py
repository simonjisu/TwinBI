# unique_index.py  (key-only: on-disk unique string set)
from __future__ import annotations
import os
import lmdb
from typing import Iterable, Iterator, Optional, Tuple, Union

ValLike = Union[str, int, float]

class UniqueIndex:
    def __init__(
        self,
        path: str,
        *,
        map_size: int = 1 << 32,
        subdir: bool = True,
        fast: bool = True,
        readahead: bool = False,
    ):
        if subdir:
            os.makedirs(path, exist_ok=True)

        env_kwargs = dict(
            map_size=map_size,
            subdir=subdir,
            max_dbs=1,
            readahead=readahead,
        )
        if fast:
            env_kwargs.update(dict(
                writemap=True,
                map_async=True,
                metasync=False,
                sync=False,
            ))

        self.env = lmdb.open(path, **env_kwargs)
        self.db  = self.env.open_db(b"set")

    @staticmethod
    def _to_str(v: ValLike) -> str:
        return v if isinstance(v, str) else str(v)

    @staticmethod
    def _enc(s: str) -> bytes:
        return s.encode("utf-8")

    @staticmethod
    def _dec(b: bytes) -> str:
        return b.decode("utf-8")

    # ---- Basic operations ----
    def add(self, value: ValLike) -> bool:
        """
        Add a key. Returns False if it already exists, True if newly inserted.
        """
        vb = self._enc(self._to_str(value))
        with self.env.begin(write=True, db=self.db) as txn:
            if txn.get(vb) is not None:
                return False
            txn.put(vb, b"")  # keep the value empty to minimize storage
            return True

    def add_many(self, values: Iterable[ValLike]) -> int:
        """
        Bulk insert: return the number of actually inserted keys after excluding duplicates/existing keys.
        - Filter existing keys before LMDB putmulti to balance performance and correctness.
        """
        # 1) Deduplicate in Python first and encode as UTF-8
        enc = []
        seen = set()
        for v in values:
            b = self._enc(self._to_str(v))
            if b not in seen:
                seen.add(b)
                enc.append(b)

        if not enc:
            return 0

        inserted = 0
        with self.env.begin(write=True, db=self.db) as txn:
            cur = txn.cursor()
            # 2) Filter out keys that already exist
            new_items = []
            for b in enc:
                if txn.get(b) is None:
                    new_items.append((b, b""))
            if not new_items:
                return 0
            # 3) Batch write
            cur.putmulti(new_items, dupdata=False, overwrite=False)
            inserted = len(new_items)
        return inserted

    def exists(self, value: ValLike) -> bool:
        vb = self._enc(self._to_str(value))
        with self.env.begin(db=self.db) as txn:
            return txn.get(vb) is not None

    def remove(self, value: ValLike) -> bool:
        vb = self._enc(self._to_str(value))
        with self.env.begin(write=True, db=self.db) as txn:
            return txn.delete(vb)

    def __len__(self) -> int:
        with self.env.begin(db=self.db) as txn:
            return txn.stat()["entries"]

    def __iter__(self) -> Iterator[str]:
        with self.env.begin(db=self.db) as txn:
            cur = txn.cursor()
            if cur.first():
                yield self._dec(cur.key())
                while cur.next():
                    yield self._dec(cur.key())

    def range_search(self, start: Optional[str], end: Optional[str]) -> Iterator[str]:
        """
        Lexicographic range search: start <= key < end (or to the end if end=None)
        """
        start_b = self._enc(start) if start is not None else b""
        end_b   = self._enc(end) if end is not None else None
        with self.env.begin(db=self.db) as txn:
            cur = txn.cursor()
            ok = cur.set_range(start_b) if start_b else cur.first()
            while ok:
                k = cur.key()
                if end_b is not None and k >= end_b:
                    break
                yield self._dec(k)
                ok = cur.next()

    def prefix_search(self, prefix: str) -> Iterator[str]:
        return self.range_search(prefix, prefix + "\uffff")

    def sync(self):
        self.env.sync()

    def close(self):
        self.env.sync()
        self.env.close()

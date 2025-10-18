# unique_index.py  (키 전용: on-disk unique string set)
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

    # ---- 기본 연산 ----
    def add(self, value: ValLike) -> bool:
        """
        키를 추가. 이미 있으면 False, 새로 추가되면 True.
        """
        vb = self._enc(self._to_str(value))
        with self.env.begin(write=True, db=self.db) as txn:
            if txn.get(vb) is not None:
                return False
            txn.put(vb, b"")  # value는 빈 바이트로 최소화
            return True

    def add_many(self, values: Iterable[ValLike]) -> int:
        """
        대량 삽입(배치): 중복/기존 키를 제외하고 실제로 추가된 개수 반환.
        - LMDB 배치 putmulti를 쓰기 전에, 존재 여부를 빠르게 걸러 성능과 정확도를 균형화.
        """
        # 1) 파이썬에서 먼저 중복 제거 + UTF-8 인코딩
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
            # 2) 기존 존재 여부 필터링
            new_items = []
            for b in enc:
                if txn.get(b) is None:
                    new_items.append((b, b""))
            if not new_items:
                return 0
            # 3) 배치 쓰기
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
        사전식 범위 검색: start <= key < end (end=None이면 끝까지)
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

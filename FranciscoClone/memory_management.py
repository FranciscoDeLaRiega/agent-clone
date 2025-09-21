import json
import re
import os
import tempfile
from threading import RLock
from typing import Any, Dict, List, Optional
from pathlib import Path


_PAIR_RE = re.compile(r"(?<!\d)(\d{2,})\s*(?:↔|<->|->|=>|→|—|–|:|-|,|\||/|\\)\s*(\d{2,})(?!\d)")
_PAIR_FALLBACK_RE = re.compile(r"(?<!\d)(\d{2,})\D{1,6}(\d{2,})(?!\d)")

class MemoryStore:
    """
    JSON-backed store for user notes, keeping the last 100 per user.
    Each user is identified by a caller-provided key (defaults to 'global').
    """

    def __init__(self, path: Optional[str] = None) -> None:
        default_path = Path(os.path.expanduser("~")) / ".francisco_agent_memory.json"
        self._path = Path(path).expanduser() if path else default_path
        self._lock = RLock()

        #Ensure the file exists with an empty dict
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if not self._path.exists():
                self._atomic_write({})
        except Exception:
            self._path = Path()

    #internal I/O
    def _read(self) -> Dict[str, Any]:
        with self._lock:
            if not self._path:
                return {}
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                try:
                    self._path.rename(self._path.with_suffix(self._path.suffix + ".corrupt"))
                except Exception:
                    pass
                self._atomic_write({})
                return {}
            except FileNotFoundError:
                self._atomic_write({})
                return {}
            except Exception:
                return {}

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        """Write JSON atomically: write to temp file, then replace target."""
        if not self._path:
            return
        dirpath = self._path.parent
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dirpath, delete=False) as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, self._path)

    def _write(self, data: Dict[str, Any]) -> None:
        with self._lock:
            try:
                self._atomic_write(data)
            except Exception:
                pass

    #public API
    @staticmethod
    def get_user_key(metadata: Optional[Dict[str, Any]]) -> str:
        if not metadata:
            return "global"
        user_id = metadata.get("user_id") or metadata.get("userId") or metadata.get("uid")
        return str(user_id) if user_id else "global"

    def get_notes(self, user_key: str) -> List[str]:
        data = self._read()
        notes = data.get(user_key, {}).get("notes", [])
        if isinstance(notes, list):
            return [str(n) for n in notes if isinstance(n, str)]
        return []

    def append_note(self, user_key: str, note: str) -> None:
        if not note:
            return
        data = self._read()
        user_obj = data.get(user_key) or {}
        notes = user_obj.get("notes")
        if not isinstance(notes, list):
            notes = []
        notes.append(note)
        user_obj["notes"] = notes[-100:]
        data[user_key] = user_obj
        self._write(data)

    def summary(self, user_key: str, max_chars: int = 1000) -> str:
        notes = self.get_notes(user_key)
        if not notes:
            return ""
        joined = "\n".join(f"- {n}" for n in notes)
        return joined if len(joined) <= max_chars else joined[-max_chars:]
    
    def _ensure_user_obj(self, data: Dict[str, Any], user_key: str) -> Dict[str, Any]:
        obj = data.get(user_key)
        if not isinstance(obj, dict):
            obj = {}
        if "pairs" not in obj or not isinstance(obj["pairs"], dict):
            obj["pairs"] = {}
        if "notes" not in obj or not isinstance(obj["notes"], list):
            obj["notes"] = []
        data[user_key] = obj
        return obj

    def set_pair(self, user_key: str, a: str, b: str) -> None:
        """Store bi-directional mapping a<->b as strings."""
        if not a or not b:
            return
        data = self._read()
        obj = self._ensure_user_obj(data, user_key)
        pairs: Dict[str, str] = obj["pairs"]
        pairs[str(a)] = str(b)
        pairs[str(b)] = str(a)
        self._write(data)

    def find_pair(self, user_key: str, key: str) -> Optional[str]:
        data = self._read()
        obj = data.get(user_key) or {}
        pairs = obj.get("pairs") or {}
        if not isinstance(pairs, dict):
            return None
        val = pairs.get(str(key))
        return str(val) if val is not None else None

    def ingest_text_for_pairs(self, user_key: str, text: str) -> None:
        if not text:
            return
        matched = False
        for m in _PAIR_RE.finditer(text):
            a, b = m.group(1), m.group(2)
            self.set_pair(user_key, a, b)
            matched = True
        if not matched:
            for m in _PAIR_FALLBACK_RE.finditer(text):
                a, b = m.group(1), m.group(2)
                self.set_pair(user_key, a, b)

    def rebuild_pairs_from_all_notes(self) -> None:
        """Scan all users' notes for number pairs and index them into pairs bi-directionally."""
        data = self._read()
        changed = False
        for user_key, obj in list(data.items()):
            if not isinstance(obj, dict):
                continue
            notes = obj.get("notes") or []
            if not isinstance(notes, list):
                continue
            pairs = obj.get("pairs")
            if not isinstance(pairs, dict):
                pairs = {}
                obj["pairs"] = pairs
            for note in notes:
                if not isinstance(note, str):
                    continue
                #use both regexes
                for rx in (_PAIR_RE, _PAIR_FALLBACK_RE):
                    for m in rx.finditer(note):
                        a, b = m.group(1), m.group(2)
                        if pairs.get(a) != b or pairs.get(b) != a:
                            pairs[a] = b
                            pairs[b] = a
                            changed = True
            data[user_key] = obj
        if changed:
            self._write(data)

    def find_pair_any(self, key: str) -> Optional[str]:
        """Search all users' pair maps; rebuild from notes if needed."""
        data = self._read()
        skey = str(key)
        #first pass: direct pairs
        for obj in data.values():
            if isinstance(obj, dict):
                pairs = obj.get("pairs") or {}
                if isinstance(pairs, dict) and skey in pairs:
                    return str(pairs[skey])
        #second pass: rebuild from notes, then try again
        self.rebuild_pairs_from_all_notes()
        data = self._read()
        for obj in data.values():
            if isinstance(obj, dict):
                pairs = obj.get("pairs") or {}
                if isinstance(pairs, dict) and skey in pairs:
                    return str(pairs[skey])
        return None



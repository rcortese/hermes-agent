"""Exact-pair CAS for independent Codex rows; unrelated pool semantics stay upstream."""
_TOKEN_FIELDS = ("access_token", "refresh_token", "last_refresh", "id_token", "expires_at", "expires_at_ms")


def merge_manual_codex_generations(provider, entries, disk_by_id, token_bases, known_ids, removed):
    merged = []
    for entry in entries:
        if not isinstance(entry, dict):
            merged.append(entry)
            continue
        eid = entry.get("id")
        if eid in removed:
            continue
        disk = disk_by_id.get(eid)
        if (provider == "openai-codex" and entry.get("source") == "manual:device_code"
                and entry.get("auth_type") == "oauth"):
            if disk is None and (eid in known_ids or eid in token_bases):
                continue
            if disk is not None and token_bases.get(eid) != (disk.get("access_token"), disk.get("refresh_token")):
                entry = dict(entry)
                for field in _TOKEN_FIELDS:
                    if field in disk:
                        entry[field] = disk[field]
                    else:
                        entry.pop(field, None)
        merged.append(entry)
    return merged

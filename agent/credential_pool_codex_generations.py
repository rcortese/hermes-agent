"""Shared independent Codex generations, ported from Moss 27122a5.

Lock order stays pool RLock -> owning store flock. No singleton authority for
manual accounts; force on a stale pair adopts the winner without another POST.
"""
from dataclasses import replace


def persist_manual_pool(pool, *, removed_ids=None, token_bases=None, status_cleared_ids=None):
    from agent import credential_pool as cp
    from hermes_cli import auth
    with cp._auth_store_lock():
        auth.write_credential_pool(pool.provider, [e.to_dict() for e in pool._entries],
            removed_ids=removed_ids, token_bases=token_bases,
            known_ids=pool._known_manual_ids, status_cleared_ids=status_cleared_ids)
        persisted = {r.get("id"): r for r in auth.read_credential_pool(pool.provider) if isinstance(r, dict)}
        entries = []
        for entry in pool._entries:
            if entry.source != "manual:device_code" or entry.auth_type != cp.AUTH_TYPE_OAUTH:
                entries.append(entry)
                continue
            row = persisted.get(entry.id)
            if row is not None:
                entries.append(cp.PooledCredential.from_dict(pool.provider, row))
                pool._known_manual_ids.add(entry.id)
            elif entry.id not in pool._known_manual_ids and entry.id not in (token_bases or {}):
                entries.append(entry)
            elif pool._current_id == entry.id:
                pool._current_id = None
        pool._entries = entries


def refresh_manual_entry(pool, entry, *, force):
    from agent import credential_pool as cp
    with cp._auth_store_lock(timeout_seconds=pool._single_use_refresh_lock_timeout()):
        row = next((r for r in cp.read_credential_pool(pool.provider)
                    if isinstance(r, dict) and r.get("id") == entry.id), None)
        if row is None:
            pool._entries = [e for e in pool._entries if e.id != entry.id]
            pool._known_manual_ids.add(entry.id)
            if pool._current_id == entry.id:
                pool._current_id = None
            return None
        stored = cp.PooledCredential.from_dict(pool.provider, row)
        pool._replace_entry(entry, stored)
        pool._known_manual_ids.add(entry.id)
        if (stored.access_token, stored.refresh_token) != (entry.access_token, entry.refresh_token):
            return stored
        if not stored.refresh_token:
            if force:
                pool._mark_exhausted(stored, None)
            return None
        old_pair = (stored.access_token, stored.refresh_token)
        try:
            refreshed = cp.auth_mod.refresh_codex_oauth_pure(stored.access_token, stored.refresh_token)
        except Exception:
            pool._mark_exhausted(stored, None)
            return None
        updated = replace(stored, access_token=refreshed["access_token"], refresh_token=refreshed["refresh_token"],
            last_refresh=refreshed.get("last_refresh"), last_status=cp.STATUS_OK, last_status_at=None,
            last_error_code=None, last_error_reason=None, last_error_message=None, last_error_reset_at=None)
        pool._replace_entry(stored, updated)
        pool._persist(token_bases={stored.id: old_pair})
        return next((e for e in pool._entries if e.id == stored.id), None)

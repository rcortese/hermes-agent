# Restricted A2A profile plugin

`a2a-platform` is a Hermes `platform` plugin with two separately declared surfaces:

- **Outbound tool surface:** five core-owned tools in toolset `a2a`:
  `a2a_call(message[, context_id])`, `a2a_discover()`, `a2a_list()`,
  `a2a_history(context_id[, limit])`, and `a2a_orchestrate(message[, context_id])`.
  Calls and orchestration always use the single `denholm` alias at
  `http://denholm:9900`; orchestration is one delegation, not fan-out.
  Discovery/list are fixed local descriptions rather than remote discovery or
  enumeration, and history reads only local persisted context records.
  Callers cannot choose an agent, URL, path, or credential.
- **Inbound adapter:** platform `a2a`, retaining authenticated A2A task reception
  through the Hermes gateway adapter.

The plugin does **not** perform remote discovery, remote peer listing, remote
history reads, direct-URL routing, or fan-out. Conversation persistence and
protocol support used internally by the admitted calls and inbound adapter are
not an authority to contact another peer.

## Configuration

```yaml
plugins:
  enabled: [a2a-platform]
  disabled: []
a2a_agents:
  denholm:
    auth: {type: bearer, token: "${MOSS_TO_DENHOLM_A2A_TOKEN}"}
    timeout: 120
```

The outbound tool posts only to its fixed endpoint. It does not use Agent Card
discovery, accept caller routing/credentials, or let redirects replace or extend
endpoint authority. The inbound listener remains on port `9900` and Docker
runtime configuration must keep that listener Docker-internal (no host port
publication).

A name in `plugins.disabled` wins over `plugins.enabled`; this restricted profile
therefore keeps the plugin enabled and not disabled. Inbound listener lifecycle
and credentials remain operator/runtime concerns; installing this source package
does not activate a listener.

The admitted plugin is image-owned at `/opt/hermes/plugins/platforms/a2a`. A
patched protected Hermes loader is the primary collision-rejection boundary, with
the fixed startup guard as defense in depth. The profile materializer never
installs this plugin. Source and profile-lock validation do not claim runtime
readiness; deployment-image binding and runtime E2E evidence remain pending.

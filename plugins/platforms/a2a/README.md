# Restricted A2A profile plugin

`a2a-platform` is a Hermes `platform` plugin with two separately declared surfaces:

- **Outbound tool surface:** only `a2a_call(message[, context_id])` in toolset `a2a`.
  It always calls the `denholm` alias at `http://denholm:9900`; callers cannot
  choose an agent or URL.
- **Inbound adapter:** platform `a2a`, retaining authenticated A2A task reception
  through the Hermes gateway adapter.

The plugin does **not** register discovery, list, history, orchestration, or fan-out tools.
Conversation persistence and protocol support used internally by the admitted call
and inbound adapter are not additional model-visible tools.

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
discovery, and redirects cannot replace or extend endpoint authority.

A name in `plugins.disabled` wins over `plugins.enabled`; this restricted profile
therefore keeps the plugin enabled and not disabled. Inbound listener lifecycle
and credentials remain operator/runtime concerns; installing this source package
does not activate a listener.

The admitted plugin is image-owned at `/opt/hermes/plugins/platforms/a2a`. A
patched protected Hermes loader is the primary collision-rejection boundary, with
the fixed startup guard as defense in depth. The profile materializer never
installs this plugin. Source and profile-lock validation do not claim runtime
readiness; deployment-image binding and runtime E2E evidence remain pending.

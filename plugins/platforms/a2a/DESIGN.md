# Restricted A2A design

## Authority boundary

The package name is `a2a-platform`, its Hermes plugin kind is `platform`, and its
normative outbound registration set is exactly `[a2a_call]`. The tool accepts
only a message (and optional continued-exchange context id) and always calls
the `denholm` alias at `http://denholm:9900`; configuration supplies only its
bearer credential reference and timeout. The core-owned A2A API cannot be
replaced or expanded by a plugin.

## Separate inbound surface

The plugin also registers the `a2a` inbound platform adapter. This is not an
outbound model tool. The adapter preserves authenticated task reception, inbound
identity/rate-limit checks, and gateway dispatch. Listener activation and secrets
are external runtime responsibilities.

## Loader boundary

The admitted payload is an immutable, image-owned plugin at
`/opt/hermes/plugins/platforms/a2a`. A patched protected Hermes loader is the
primary enforcement boundary: it admits that fixed path for the `a2a-platform`
key and rejects project, profile-local, or `hermes_agent.plugins` entry-point
collisions rather than allowing them to supersede it. The fixed launcher startup
guard verifies path, ownership, writability, and collision conditions as defense
in depth; it does not replace loader enforcement.

The profile materializer never installs or mutates this plugin. The plugin remains
in `plugins.enabled` and absent from `plugins.disabled`, because the disabled list
has deny precedence. Source sealing alone does not establish runtime readiness:
the deployment lock, loader postimage, immutable image identity, and runtime E2E
receipt remain external gates.

## Non-authoritative donor

The installed Hermes package may provide implementation ancestry, but its bytes
and release metadata are not authority for this restricted profile package.
Only the repository source package and its consuming profile lock can define the
restricted payload supplied to the protected image build. Installation authority
belongs to the reviewed immutable deployment image, never to the profile
materializer.

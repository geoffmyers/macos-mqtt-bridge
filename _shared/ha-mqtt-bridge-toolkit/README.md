# ha-mqtt-bridge-toolkit

Shared utilities for Python bridges that publish to Home Assistant over MQTT:
synchronous and threaded bridges built on paho-mqtt, and asyncio bridges built
on aiomqtt. A bridge that uses it carries a copy under
`_shared/ha-mqtt-bridge-toolkit/`.

The toolkit is mostly pure-data helpers (build a dict, format a string) but
also ships two stateful pieces — a disk-backed outbox queue and a threaded
paho-mqtt v5 publisher — because the bridges all reimplemented those the
same way.

## Modules

| Module | Purpose |
|---|---|
| `discovery` | `build_discovery_payload()`, `build_device_block()`, `availability_block()` — pure functions returning HA MQTT Discovery dicts. |
| `topics` | `slugify()`, `slugify_hostname()`, `state_topic()`, `discovery_topic()`, `event_topic()` — topic + identifier string helpers. |
| `time_utils` | `epoch_to_iso()`, `epoch_ms_to_iso()`, `iso_now()`, `iso_from_monotonic()` — ISO-8601 UTC formatting with ms precision. |
| `outbox` | `Outbox` — disk-backed JSONL FIFO queue with size cap, drain-on-ACK, head-of-line preservation. Survives process restarts. |
| `paho_publisher` | `ThreadedPublisher` — paho-mqtt v5 wrapper with LWT, optional TLS (`tls=`, `ca_file=`), subscribe map, and five publish flavors (`publish_event` / `publish_event_with_ack` / `publish_state` / `publish_attributes` / `publish_discovery` / `publish_raw`). Its health file (`health_path`) is touched only while the broker is connected, so a file-age healthcheck fails when the broker goes away. `watch_ha_birth()` calls back whenever Home Assistant publishes `online` on `<prefix>/status`, so a bridge can re-send discovery after HA restarts. Subclass for bridge-specific behavior. |
| `http_retry` | `request_with_backoff()` — one HTTP request with exponential backoff on 429/5xx; raises `RetryExhaustedError` (a `RuntimeError`) when it gives up. Needs the `[http]` extra (`requests`). |
| `aiomqtt_helpers` | `mqtt_client_kwargs()` — generic builder for `aiomqtt.Client(**kwargs)` construction. For asyncio bridges. |
| `config_helpers` | `substitute_env_vars()`, `load_yaml_with_env()` — YAML config + `${ENV_VAR}` substitution. PyYAML is loaded lazily; install via the `[yaml]` extra. |
| `app_helpers` | `configure_logging()`, `register_github_error_reporter()` — process-startup boilerplate every bridge `main()` repeated (uniform `basicConfig` + named logger; optional GitHub error reporter, no-op when its package is absent). |

## Dependencies

- **Required:** `paho-mqtt>=2.1.0` (needed for `ThreadedPublisher`).
- **Optional:** `pyyaml>=6.0.1` via the `[yaml]` extra (only needed for
  `load_yaml_with_env`).
- `aiomqtt` is NOT a dep — the toolkit's `mqtt_client_kwargs()` builds the
  kwargs dict but doesn't import aiomqtt. The caller imports aiomqtt and
  passes the dict.

## Upgrading a consumer

A Docker image that installs the toolkit at build time keeps the version it was
built with: restarting the container does not pick up a toolkit change, and
bridge code that imports something new (such as `http_retry`) fails to start
until the image is rebuilt. Rebuild and recreate the container after changing
the toolkit.

## Usage

### HA Discovery payload

```python
from ha_mqtt_bridge import (
    availability_block,
    build_device_block,
    build_discovery_payload,
    discovery_topic,
)

device = build_device_block(
    identifiers=["govee_h5054_ABCD"],
    name="Kitchen Leak Sensor",
    manufacturer="Govee",
    model="H5054",
    sw_version="1.2.3",
)
payload = build_discovery_payload(
    name="Leak",
    unique_id="govee_h5054_ABCD_leak",
    object_id="govee_h5054_ABCD_leak",
    state_topic="govee/leak/ABCD/leak",
    device=device,
    device_class="moisture",
    payload_on="ON",
    payload_off="OFF",
    component="binary_sensor",
    **availability_block("govee/leak/bridge/online"),
)
topic = discovery_topic("homeassistant", "binary_sensor", "govee_h5054_ABCD/leak")
```

### Threaded paho publisher

```python
from ha_mqtt_bridge import ThreadedPublisher

pub = ThreadedPublisher(
    host="mosquitto",
    port=1883,
    username="user",
    password="pass",
    client_id="my-bridge",
    lwt_topic="my-bridge/online",   # publishes "online" on connect, "offline" on disconnect
)
pub.start()
pub.publish_state("sensors/temp", "21.5")
pub.publish_discovery(component="sensor", unique_id="my_temp", payload={...})
pub.stop()
```

### Disk-backed outbox

```python
from ha_mqtt_bridge import Outbox

outbox = Outbox("/var/lib/my-bridge/outbox.jsonl", max_pending=50_000)
outbox.enqueue("topic/foo", {"event": "data"})

# Drain when the broker comes back; publish_fn returns True on broker ACK.
def publish_fn(topic, payload, retain):
    return pub.publish_event_with_ack(topic, payload, retain=retain)

drained = outbox.drain(publish_fn)
```

### aiomqtt kwargs

```python
import aiomqtt
from ha_mqtt_bridge import mqtt_client_kwargs

async with aiomqtt.Client(
    **mqtt_client_kwargs(
        hostname="mqtt.example.com",
        port=8883,
        username="user",
        password="pass",
        tls=True,  # default SSLContext: system trust + hostname check + CERT_REQUIRED
    )
) as client:
    await client.publish("topic/foo", payload="bar")
```

### YAML config with env substitution

```python
from ha_mqtt_bridge import load_yaml_with_env

# config.yaml uses ${MQTT_USERNAME}, ${MQTT_PASSWORD}, etc.
data = load_yaml_with_env("config.yaml")
# → dict with all ${UPPER_SNAKE} placeholders expanded from os.environ.
# Missing env vars raise KeyError at load time.
```

## Installing into a bridge

A bridge declares the toolkit as a relative-path dependency on its copy.

**Docker** (Compose `additional_contexts`, so the build context stays narrow):

```yaml
# docker-compose.yml
build:
  context: .
  additional_contexts:
    ha_mqtt_bridge: ./_shared/ha-mqtt-bridge-toolkit
```

```dockerfile
# Dockerfile
COPY --from=ha_mqtt_bridge . /opt/ha-mqtt-bridge-toolkit
RUN pip install --no-cache-dir /opt/ha-mqtt-bridge-toolkit
```

**uv** (`pyproject.toml` path source):

```toml
[project]
dependencies = [
    "ha-mqtt-bridge-toolkit[yaml]>=0.4.0",
]

[tool.uv.sources]
ha-mqtt-bridge-toolkit = { path = "./_shared/ha-mqtt-bridge-toolkit", editable = true }
```

**pip** (`requirements.txt` path line):

```
./_shared/ha-mqtt-bridge-toolkit
```

## Tests

From this directory:

```bash
pip install -e '.[dev]'
pytest
```

Each module has its own test file pinning the wire-level contract the bridges
depend on.

## Version history

- **0.5.0** — `app_helpers` (`configure_logging` + `register_github_error_reporter`),
  the startup boilerplate the Docker bridges each repeated.
- **0.4.0** — `aiomqtt_helpers` and `config_helpers`.
- **0.3.0** — `paho_publisher.ThreadedPublisher`, the shared paho v5 wrapper.
- **0.2.0** — `outbox.Outbox`.
- **0.1.0** — `discovery`, `topics`, `time_utils`.

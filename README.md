# Ecovacs DEEBOT T90S for Home Assistant

Unofficial Home Assistant custom integration for Ecovacs DEEBOT robots, made
for the **DEEBOT T90S PRO OMNI** (Care Kit). It combines in one config entry
and one device per robot:

- everything the built-in Home Assistant **Ecovacs** integration offers:
  vacuum, map image, sensors, buttons, selects, numbers, switches, the last
  job event and the `raw_get_positions` action;
- the **camera live view** of
  [ECOVACS Live View](https://github.com/imcdo17-cyber/home-assistant-ecovacs-live):
  camera, start/stop buttons, a live view switch and a status sensor;
- built-in support for the **DEEBOT T90S PRO OMNI** (device class `833gj5`),
  which `deebot-client` does not know yet;
- a debug action, `ecovacs_t90s.raw_command`, that sends any JSON command to
  the robot and returns the raw answer.

You log in once; the robot control and the live view share that login.

> Status: phase 1. Tested with mocked Ecovacs APIs only, see "Known
> limitations".

## Requirements

- Home Assistant 2026.11 or newer.
- An Ecovacs account with an MQTT (JSON) robot. Old XMPP robots are not
  supported; use the built-in Ecovacs integration for those.
- For the live view: the Live View PIN that protects the robot camera in the
  Ecovacs app.

## Installation

### HACS (custom repository)

1. HACS > menu > **Custom repositories**.
2. Add `https://github.com/basschoren/ha-ecovacs-t90s` with category
   **Integration**.
3. Install **Ecovacs DEEBOT T90S** and restart Home Assistant.

### Manual

Copy `custom_components/ecovacs_t90s/` to `/config/custom_components/` and
restart Home Assistant.

## Configuration

**Settings > Devices & services > Add integration > Ecovacs DEEBOT T90S**

1. Choose **Cloud** (or **Self-hosted** if you run your own Ecovacs-compatible
   server; the live view needs the Ecovacs cloud).
2. Enter your Ecovacs username, password and country, and optionally the Live
   View PIN.
3. If Ecovacs asks to verify Home Assistant as a new device, enter the code it
   e-mails you.

**Configure** (options) lets you set or change the Live View PIN and the time
after which a live view stops automatically (default 10 minutes, 0 = never).
Changing the options does not reconnect the robot.

### Migrating from Ecovacs + ECOVACS Live View

This integration uses its own domain (`ecovacs_t90s`), so it can run next to
the built-in `ecovacs` integration and `ecovacs_live` while you compare them.
It gets its own devices and entity IDs; nothing is migrated automatically.
When you are done:

- remove the `ecovacs_live` config entry (two live view logins for one
  robot are not useful);
- remove the helper that patched `deebot-client` for class `833gj5`; this
  integration does that itself;
- remove the built-in `ecovacs` config entry if you no longer need it.

## Using the live view

Press **Start live view** (or turn on the **Live view** switch), wait until
**Live view status** shows *Streaming*, and open the **Live view** camera.
Press **Stop live view** or turn off the switch when you are done. The camera
shows the last frame (or a placeholder) while no session runs; it never
starts a session by itself.

## Debug action: `ecovacs_t90s.raw_command`

Sends a JSON command through `deebot-client` and returns the raw answer. It is
meant for exploring undocumented commands. A wrong command can change robot
settings, so be careful with `set...` commands.

```yaml
action: ecovacs_t90s.raw_command
target:
  entity_id: vacuum.my_robot      # or device_id of the robot
data:
  command: getCleanInfo_V2
  params: {}                      # optional JSON object or list
  timeout: 30                     # optional, seconds (1-300)
response_variable: answer
```

The response always contains `success`, `command`, `params`, `device_class`
and `elapsed_seconds`, plus either `response` (the raw API answer, also when
the robot reports an error) or `error` and `message` (timeout, API or
connection errors). Select exactly one robot.

## Supporting another robot model

If `deebot-client` does not know your robot, the log shows
`Device class "xxxxxx" not recognized`. If the robot is a variant of a
supported model, add a line to `EXTRA_DEVICE_CLASSES` in
`custom_components/ecovacs_t90s/device_classes.py`:

```python
EXTRA_DEVICE_CLASSES = {
    "833gj5": "twunby",  # DEEBOT T90S PRO OMNI (Care Kit) -> DEEBOT T90 PRO OMNI
    "xxxxxx": "yyyyyy",  # your model -> supported model with the same features
}
```

The mapping is skipped automatically once `deebot-client` supports a class
natively. `deebot-client` has no public API for this, so the integration fills
its internal capability cache; if that cache changes in a future release, a
warning is logged and the robot is reported as unsupported instead of breaking
setup. Diagnostics show the status of each mapping.

## Privacy and diagnostics

The repository contains no account data or robot identifiers. Your credentials,
the renewable Ecovacs account token and the Live View PIN are stored in the
Home Assistant config entry, like other integrations do. Diagnostics redact
the username, password, tokens, client device ID, PIN, robot IDs, serial
numbers and robot names. Debug logs of `deebot-client` can contain tokens;
review them before sharing.

## Known limitations

- Phase 1: verified with mocked APIs and an import/setup smoke test, not yet
  on a real robot.
- The live view uses private Ecovacs/AWS Kinesis APIs and can break when
  Ecovacs changes them.
- Robots with class `833gj5` use the capabilities of the T90 PRO OMNI; features
  unique to the T90S are not exposed yet.

## Credits and license

Licensed under the Apache License 2.0, see [LICENSE](LICENSE). Contains code
from the Home Assistant core `ecovacs` integration (Apache-2.0) and from
ECOVACS Live View (MIT); see [NOTICE](NOTICE). Not affiliated with ECOVACS or
the Home Assistant project.

## Nederlands

Onofficiële Home Assistant-integratie voor Ecovacs DEEBOT-robots, gemaakt voor
de **DEEBOT T90S PRO OMNI**. Eén configuratie en één apparaat per robot met
alles van de ingebouwde Ecovacs-integratie, plus het live camerabeeld van
ECOVACS Live View en ondersteuning voor de T90S (apparaatklasse `833gj5`).

- **Installeren:** via HACS als aangepaste repository
  (`https://github.com/basschoren/ha-ecovacs-t90s`, categorie Integratie) of
  door `custom_components/ecovacs_t90s/` naar `/config/custom_components/` te
  kopiëren. Herstart daarna Home Assistant.
- **Instellen:** voeg **Ecovacs DEEBOT T90S** toe, kies **Cloud** en log één
  keer in met je Ecovacs-account en land. De Live View-pincode is optioneel en
  kan later via **Configureren** worden ingesteld, net als na hoeveel minuten
  live view automatisch stopt.
- **Live view:** druk op **Live view starten** of zet de schakelaar **Live
  view** aan en open de camera zodra de status *Live* is.
- **Overstappen:** de integratie kan naast `ecovacs` en `ecovacs_live` draaien.
  Verwijder daarna `ecovacs_live` en het hulpje dat `deebot-client` voor de
  T90S aanpaste; dat doet deze integratie zelf.
- **Debuggen:** de actie `ecovacs_t90s.raw_command` stuurt een willekeurig
  JSON-commando naar de robot en geeft het onbewerkte antwoord terug.

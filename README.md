# Ortsnetz Sender

Python-Skript zur Übertragung von Netzspannungsdaten eines Carlo-Gavazzi-EM24-Zählers an [ortsnetz-auslastung.de](https://ortsnetz-auslastung.de).

> **Status:** Community-Integration in experimentellem Stand. Dieses Repository ist kein offizieller Bestandteil von ortsnetz-auslastung.de. Eine offizielle Unterstützung oder Kompatibilität mit der aktuellen API ist nicht zugesichert. Vor dem produktiven Einsatz bitte die aktuellen API-Vorgaben des Projekts prüfen.

## Zweck

Das Skript liest Messwerte aus MQTT (bereitgestellt durch `mbmd`) und sendet sie an die Messdaten-API von ortsnetz-auslastung.de. Es ist für Linux-Systeme wie Raspberry Pi OS bzw. Debian gedacht.

Vorgesehene Umgebung:

- Smartmeter: Carlo Gavazzi EM24, ausgelesen über `mbmd`
- MQTT-Broker: standardmäßig `127.0.0.1:1883`
- Übertragung: standardmäßig alle fünf Minuten
- Optionale PV-Tagesprognose über die Open-Meteo-API
- Python-Standardbibliothek; keine zusätzlichen Python-Pakete

## Funktionsumfang

- Liest die drei Phasenspannungen L1–L3 und optional die Netzfrequenz aus MQTT.
- Prüft, ob alle drei Spannungswerte vorhanden sind und zwischen 150 V und 300 V liegen.
- Ergänzt Zeitstempel in UTC, Standortkoordinaten, Anlagenleistung, Zählermodell und Integrationsversion.
- Berechnet eine grobe PV-Tagesprognose anhand der von Open-Meteo gelieferten täglichen Globalstrahlung. Diese Schätzung ersetzt keine Anlagen- oder Ertragsprognose.
- Protokolliert Übertragungsfehler und die von der API zurückgelieferte Statusampel.

Die verwendete Näherung lautet:

`Tagesertrag (kWh) = Globalstrahlung (MJ/m²) / 3,6 × Anlagenleistung (kWp) × Performance-Faktor`

Der Performance-Faktor ist standardmäßig `0.85`. Die Prognose wird höchstens alle drei Stunden neu abgefragt. Wenn Open-Meteo nicht erreichbar ist oder keine brauchbaren Daten liefert, wird das Prognosefeld ausgelassen, sofern kein Cache-Wert für denselben Tag vorliegt.

## Voraussetzungen

- Linux-System mit Python 3.9 oder neuer
- `mbmd`, so konfiguriert, dass die benötigten Messwerte an MQTT veröffentlicht werden
- Erreichbarer MQTT-Broker
- `mosquitto_sub` (unter Debian/Raspberry Pi OS im Paket `mosquitto-clients`)
- Internetzugang für die API-Übertragung und optional Open-Meteo

Installation des MQTT-Clients:

```bash
sudo apt update
sudo apt install mosquitto-clients
```

## Einrichtung

### 1. Repository herunterladen

Repository klonen oder die Dateien auf das Zielsystem kopieren. Das Skript benötigt keine zusätzlichen Python-Pakete.

### 2. Konfiguration kontrollieren

Öffne `ortsnetz_sender.py` und kontrolliere mindestens folgende Einstellungen:

| Einstellung | Zweck |
|---|---|
| `LATITUDE`, `LONGITUDE` | Koordinaten, die an die API und Open-Meteo übermittelt werden |
| `PLANT_KWP` | Installierte PV-Anlagenleistung in kWp |
| `PERFORMANCE_FACTOR` | Vereinfachter Faktor für die PV-Prognose |
| `MQTT_HOST`, `MQTT_PORT` | Adresse und Port des MQTT-Brokers |
| `MQTT_USER`, `MQTT_PASSWORD` | Optionaler MQTT-Zugang |
| `TOPIC_PREFIX` | Präfix der von `mbmd` veröffentlichten Topics |
| `TOPIC_FREQUENCY` | Topic der Netzfrequenz; leer lassen, wenn nicht verfügbar |
| `FORECAST_ENABLED` | Aktiviert bzw. deaktiviert die Open-Meteo-Prognose |

Die im Skript hinterlegten Koordinaten, Anlagenleistung und MQTT-Topics sind installationsabhängige Werte. Vor dem Einsatz müssen sie überprüft und gegebenenfalls angepasst werden.

**Datenschutz:** Die Koordinaten werden mit den Messdaten an den Dienst übermittelt und für die Prognoseabfrage an Open-Meteo verwendet. Verwende nur Koordinaten, deren Übermittlung für dich akzeptabel ist. Prüfe außerdem, ob die Standortangabe den Vorgaben des Projekts entspricht.

### 3. MQTT-Topics überprüfen

Standardmäßig erwartet das Skript diese Topics:

```text
mbmd/cgex3x02-1/Voltage/L1
mbmd/cgex3x02-1/Voltage/L2
mbmd/cgex3x02-1/Voltage/L3
mbmd/cgex3x02-1/Frequency
```

Passe `TOPIC_PREFIX` und gegebenenfalls `TOPIC_FREQUENCY` an deine `mbmd`-Konfiguration an.

## Ausführen und testen

Skript ausführbar machen:

```bash
chmod +x ortsnetz_sender.py
```

Trockenlauf: Erstellt den Payload und gibt ihn aus, ohne ihn an die API zu senden:

```bash
./ortsnetz_sender.py --dry-run --once
```

Eine einzelne Messung senden:

```bash
./ortsnetz_sender.py --once
```

Dauerbetrieb im Vordergrund:

```bash
./ortsnetz_sender.py
```

Ausführlichere Protokollierung:

```bash
./ortsnetz_sender.py --verbose --once
```

Ein erfolgreicher HTTP-Aufruf bedeutet nicht zwangsläufig, dass die Messung fachlich akzeptiert oder dauerhaft übernommen wurde. Maßgeblich sind die Antwort der API und deren aktuelle Validierungsregeln.

## Betrieb mit systemd (optional)

Die Datei `ortsnetz-sender.service` ist eine Vorlage und muss an das eigene System angepasst werden. Insbesondere müssen der Skriptpfad und der Benutzer stimmen. Die Beispielkonfiguration verwendet `/home/pi/ortsnetz_sender.py` und den Benutzer `pi`; diese Werte passen nicht auf jedes System.

Beispielinstallation:

```bash
sudo cp ortsnetz-sender.service /etc/systemd/system/
sudo nano /etc/systemd/system/ortsnetz-sender.service
```

Prüfe und passe mindestens `ExecStart` und `User` an. Danach:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ortsnetz-sender.service
sudo systemctl status ortsnetz-sender.service
journalctl -u ortsnetz-sender.service -f
```

Zum Stoppen und Deaktivieren:

```bash
sudo systemctl disable --now ortsnetz-sender.service
```

## Bekannte Grenzen und Hinweise

- Die PV-Prognose ist eine vereinfachte Schätzung und kann deutlich vom tatsächlichen Ertrag abweichen.
- Das Skript verwendet jeweils den letzten empfangenen MQTT-Wert während des Abfragefensters; die Qualität hängt von der Aktualisierung der MQTT-Topics ab.
- Es sendet keine Messung, wenn einer der drei Spannungswerte fehlt oder außerhalb des konfigurierten Bereichs liegt.
- Die API kann Felder, Werte oder Standorte ablehnen. Die aktuelle API-Spezifikation des Projekts ist maßgeblich.
- Das Skript wurde für eine bestimmte lokale Konfiguration entwickelt. Vor einer breiteren Nutzung sollte es mit anderen `mbmd`- und MQTT-Konfigurationen getestet werden.
- Die Funktion hängt von externen Diensten und der aktuellen API ab; Änderungen dort können Anpassungen erforderlich machen.


## Lizenz

Dieses Projekt wird unter der **MIT License** veröffentlicht. Den vollständigen Lizenztext findest du in der Datei [`LICENSE`](LICENSE).

Die MIT-Lizenz erlaubt unter ihren Bedingungen unter anderem die Nutzung, Änderung und Weitergabe des Codes. Der Copyright- und Lizenzhinweis muss dabei erhalten bleiben. Die Software wird ohne Gewährleistung bereitgestellt; Details stehen in der Lizenzdatei.

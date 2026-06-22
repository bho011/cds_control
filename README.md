# Test zur steuerung der Valves und Contactoren war erfolgreich! Habe die Hardware mit Python unter kontrolle. 
# Pumpe und Ventile sind wie unten beschrieben angeschlossen so, dass RoWatter sicher nur in den Abfluss gepumpt wird bei Tests. 
# Alles andere ist genau so wie übernommen

Funktion              GPIO   Pin   Status
Mixer Refill Pump     20     38    valid / Python OK
Supply/Test Valve 6   6      31    valid / Python OK
Drain Valve 0         21    13    valid / Python OK


mqtt_sensor_bridge.py läuft jetzt als systemd-Service.
Sensorwerte werden dauerhaft per MQTT gesendet.
Node-RED Dashboard zeigt RO- und Mixing-Tank.
State-Machine sendet Prozesszustand per MQTT.
Dashboard zeigt Process State und Aktor-LEDs.
pH, Temperatur und Dissolved Oxygen werden gelesen.
EC wird gelesen, aber noch falsch skaliert dargestellt.


==================================================================

Useful Commands
Check sensor bridge service
systemctl status cds-sensor-bridge.service
Follow sensor bridge logs
journalctl -u cds-sensor-bridge.service -f
Restart sensor bridge after code changes
sudo systemctl restart cds-sensor-bridge.service
Check MQTT sensor topic
mosquitto_sub -h localhost -t cds/status/sensors -v
Check MQTT process topic
mosquitto_sub -h localhost -t cds/status/process -v
Check Python syntax
cd ~/cds_control
source .venv/bin/activate

python -m py_compile mqtt_sensor_bridge.py
python -m py_compile main_water_state_machine.py
python -m py_compile services/mqtt_publisher.py
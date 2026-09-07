"""
Sensor Publisher: Simulates ESP32 edge hardware with weather & 3 soil zones.
Connects to Mosquitto running in Docker.

Usage:
    python3 sensor_publisher.py --mode remote --broker localhost --port 1883
"""

import math
import json
import argparse
import time
import random
import paho.mqtt.client as mqtt


class EdgeDeviceSimulator:
    """Simulates ESP32 hardware, physics (drying/watering), and sensors."""
    
    def __init__(self, broker_host: str, broker_port: int):
        self.broker_host = broker_host
        self.broker_port = broker_port
        
        # Weather state (single field hub)
        self.irradiance = 0.0
        self.T_amb = 20.0
        self.humidity = 60.0
        self.wind = 1.5
        
        # 3 Field zones state
        self.zones = {
            "Zone_A": {"M_10": 60.0, "M_30": 55.0, "M_60": 50.0, "flow_rate": 0.0, "valve_open": False},
            "Zone_B": {"M_10": 45.0, "M_30": 40.0, "M_60": 38.0, "flow_rate": 0.0, "valve_open": False},
            "Zone_C": {"M_10": 75.0, "M_30": 70.0, "M_60": 68.0, "flow_rate": 0.0, "valve_open": False},
        }

        # Setup Paho MQTT Client (v2.x Standard)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ESP32_Edge_Simulator")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"[MQTT] Connected to Mosquitto Broker in Docker ({self.broker_host}:{self.broker_port})")
            # Subscribe to control command topics for all 3 zones
            self.client.subscribe("farm/zone/+/control")
            print("[MQTT] Subscribed to valve control topic: farm/zone/+/control")
        else:
            print(f"[MQTT] Connection failed with response code {rc}")

    def _on_message(self, client, userdata, msg):
        """Receives valve commands from the central controller script."""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode("utf-8"))
            # Extract zone ID from topic string (farm/zone/Zone_A/control -> Zone_A)
            zone_id = topic.split('/')[2]
            
            if zone_id in self.zones:
                command_state = payload.get("state", "OFF").upper()
                if command_state == "ON":
                    self.zones[zone_id]["valve_open"] = True
                    self.zones[zone_id]["flow_rate"] = 15.0  # Nom. flow rate 15 L/min
                    print(f"\n[VALVE SIMULATOR] 🚰 Opened valve for {zone_id}. Flow rate: 15.0 L/min")
                else:
                    self.zones[zone_id]["valve_open"] = False
                    self.zones[zone_id]["flow_rate"] = 0.0
                    print(f"\n[VALVE SIMULATOR] 🚫 Closed valve for {zone_id}. Flow rate: 0.0 L/min")
        except Exception as e:
            print(f"[ERROR] Failed to process valve command: {e}")

    def connect(self):
        self.client.connect(self.broker_host, self.broker_port, keepalive=60)
        self.client.loop_start()

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    def update_physics(self, sim_hour: float):
        """Simulates diurnal solar curves & soil moisture dynamics."""
        # 1. Weather Math (Diurnal Sine Wave)
        if 6 <= sim_hour <= 18:
            hour_angle = math.pi * (sim_hour - 6) / 12
            self.irradiance = 800 * (1 + 0.3 * math.sin(hour_angle))
            self.T_amb = 15 + 12 * math.sin(hour_angle)
            self.humidity = 40 + 30 * math.cos(hour_angle)
            self.wind = 2 + 0.5 * math.sin(hour_angle)
        else:
            self.irradiance = 0.0
            self.T_amb = 12.0
            self.humidity = 70.0
            self.wind = 1.0

        # 2. Soil Moisture Dynamics per Zone
        for zone_id, data in self.zones.items():
            # 1. WATER INPUT (Drip irrigation hits 10cm surface layer first)
            if data["valve_open"]:
                # Drip rate adds water directly to topsoil
                data["M_10"] += random.uniform(0.8, 1.2)

            # 2. PERCOLATION / DOWNWARD DRIP (Water drains from 10cm -> 30cm -> 60cm)
            # Water moves downward if upper layer is wetter than lower layer
            drainage_10_to_30 = max(0.0, (data["M_10"] - data["M_30"]) * 0.15)
            drainage_30_to_60 = max(0.0, (data["M_30"] - data["M_60"]) * 0.10)

            data["M_10"] -= drainage_10_to_30
            data["M_30"] += drainage_10_to_30 - drainage_30_to_60
            data["M_60"] += drainage_30_to_60

            # 3. EVAPOTRANSPIRATION & DEEP DRAINAGE (Losses)
            # Top layer evaporates fast; lower layers feed root uptake
            data["M_10"] -= random.uniform(0.05, 0.15)  # Evaporation
            data["M_30"] -= random.uniform(0.02, 0.08)  # Active root zone uptake
            data["M_60"] -= random.uniform(0.01, 0.03)  # Deep drainage

            # 4. BOUNDARY CLAMPING
            data["M_10"] = round(min(95.0, max(10.0, data["M_10"])), 1)
            data["M_30"] = round(min(95.0, max(10.0, data["M_30"])), 1)
            data["M_60"] = round(min(95.0, max(10.0, data["M_60"])), 1)

            
    def publish_telemetry(self):
        """Publishes live weather and zone sensor states to Mosquitto."""
        # Publish Weather
        weather_payload = {
            "irradiance": round(self.irradiance, 1),
            "T_amb": round(self.T_amb, 1),
            "humidity": round(self.humidity, 1),
            "wind": round(self.wind, 2),
        }
        self.client.publish("farm/weather", json.dumps(weather_payload))

        # Publish Zone Telemetry
        for zone_id, data in self.zones.items():
            topic = f"farm/zone/{zone_id}/sensors"
            sensor_payload = {
                "M_10": data["M_10"],
                "M_30": data["M_30"],
                "M_60": data["M_60"],
                "flow_rate": data["flow_rate"],
            }
            self.client.publish(topic, json.dumps(sensor_payload))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ESP32 Edge Device Simulator for Docker MQTT")
    parser.add_argument("--broker", default="localhost", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--interval", type=float, default=3, help="Publish interval in seconds")
    
    args = parser.parse_args()
    
    device = EdgeDeviceSimulator(args.broker, args.port)
    device.connect()
    
    sim_hour = 6.0  # Start at 06:00 AM
    print(f"\n[SIMULATOR] Streaming continuously to Mosquitto at {args.broker}:{args.port} every {args.interval}s")
    print("Press Ctrl+C to stop.\n")
    
    try:
        while True:
            device.update_physics(sim_hour)
            device.publish_telemetry()
            
            cur_h = int(sim_hour) % 24
            cur_m = int((sim_hour % 1) * 60)
            print(f"[{cur_h:02d}:{cur_m:02d}] Telemetry Published -> Weather + 3 Zones")
            
            time.sleep(args.interval)
            sim_hour += 0.05  # Increment simulation time
            if sim_hour >= 24:
                sim_hour = 0.0
                
    except KeyboardInterrupt:
        print("\n[SIMULATOR] Stopping hardware simulator...")
        device.disconnect()
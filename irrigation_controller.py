"""
OPTIMAL PREDICTIVE IRRIGATION CONTROLLER
Removes scheduled mode. Implements exponential decay modeling + anticipatory control.
Optimizes for minimal water use while maintaining soil in optimal moisture zone.
"""

import math
import json
import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from abc import ABC, abstractmethod
import threading


# ============================================================================
# MQTT CLIENT (unchanged from original)
# ============================================================================

class MQTTClient(ABC):
    """Abstract MQTT client interface"""
    
    @abstractmethod
    def publish(self, topic: str, message: dict):
        pass
    
    @abstractmethod
    def get_latest(self, topic: str) -> dict:
        pass
    
    @abstractmethod
    def disconnect(self):
        pass


class RemoteMQTTClient(MQTTClient):
    """Connects to Mosquitto MQTT broker running in Docker or cloud"""
    
    def __init__(self, broker_host: str, broker_port: int):
        try:
            import paho.mqtt.client as mqtt
            self.broker_host = broker_host
            self.broker_port = broker_port
            
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="Irrigation_Controller")
            self.connected = False
            self.received_telemetry = {}
            self.lock = threading.Lock()
            
            def on_connect(client, userdata, flags, rc, properties=None):
                if rc == 0:
                    print(f"[MQTT CONTROLLER] Connected to Mosquitto at {broker_host}:{broker_port}")
                    self.connected = True
                    self.client.subscribe("farm/weather")
                    self.client.subscribe("farm/zone/+/sensors")
                    print("[MQTT CONTROLLER] Subscribed to farm/weather and farm/zone/+/sensors")
                else:
                    print(f"[MQTT CONTROLLER] Connection failed, response code {rc}")
            
            def on_message(client, userdata, msg):
                try:
                    payload = json.loads(msg.payload.decode("utf-8"))
                    with self.lock:
                        self.received_telemetry[msg.topic] = payload
                except json.JSONDecodeError:
                    print(f"[ERROR] Could not decode JSON on topic: {msg.topic}")
            
            self.client.on_connect = on_connect
            self.client.on_message = on_message
            
            self.client.connect(broker_host, broker_port, keepalive=60)
            self.client.loop_start()
            time.sleep(1)
            
        except ImportError:
            raise RuntimeError("paho-mqtt not installed. Run: pip install paho-mqtt")
    
    def publish(self, topic: str, message: dict):
        if self.connected:
            self.client.publish(topic, json.dumps(message))
            print(f"[CONTROLLER PUBLISH] {topic} -> {json.dumps(message)}")
    
    def get_latest(self, topic: str) -> dict:
        with self.lock:
            return self.received_telemetry.get(topic, {})
    
    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()


# ============================================================================
# CROP DEFINITIONS (simplified: remove redundant ranges)
# ============================================================================

@dataclass
class CropType:
    """
    Simplified crop definition:
    - M_min: critical dry threshold (never go below)
    - M_optimal: target zone center (where you want to operate)
    - M_sat: saturation (stop irrigation)
    - root_depth_mm: effective rooting depth for ET calculation
    - Kc: crop coefficient
    """
    name: str
    Kc: float
    M_min: float
    M_optimal: float
    M_sat: float
    root_depth_mm: float = 300.0


TOMATO = CropType(
    name="tomato",
    Kc=1.0,
    M_min=25,
    M_optimal=50,
    M_sat=85,
    root_depth_mm=600,
)

LETTUCE = CropType(
    name="lettuce",
    Kc=0.8,
    M_min=30,
    M_optimal=55,
    M_sat=90,
    root_depth_mm=300,
)

OLIVE = CropType(
    name="olive",
    Kc=0.6,
    M_min=20,
    M_optimal=50,
    M_sat=80,
    root_depth_mm=900,
)


# ============================================================================
# WEATHER STATION (unchanged)
# ============================================================================

class WeatherStation:
    """Receives weather data via MQTT"""
    
    def __init__(self, mqtt_client: MQTTClient):
        self.mqtt = mqtt_client
        self.irradiance = 0
        self.T_amb = 20
        self.humidity = 60
        self.wind = 1.5
    
    def update_from_mqtt(self):
        payload = self.mqtt.get_latest("farm/weather")
        if payload:
            self.irradiance = payload.get("irradiance", 0)
            self.T_amb = payload.get("T_amb", 20)
            self.humidity = payload.get("humidity", 60)
            self.wind = payload.get("wind", 1.5)
    
    def calculate_ET0(self):
        """Penman-Monteith ET₀ (mm/hour)"""
        if self.irradiance == 0:
            return 0
        
        Rn = self.irradiance * 0.75 / 2.45e6
        es = 0.6108 * math.exp(17.27 * self.T_amb / (self.T_amb + 237.3))
        ea = es * (self.humidity / 100)
        VPD = es - ea
        delta = 4098 * es / (self.T_amb + 237.3) ** 2
        gamma = 0.067
        fu = 2.7 + 1.63 * self.wind
        
        numerator = delta * Rn + gamma * fu * VPD / (self.T_amb + 273)
        denominator = delta + gamma * (1 + 0.34 * self.wind)
        
        ET0 = numerator / denominator if denominator > 0 else 0
        return max(0, ET0)
    
    def calculate_ETc(self, crop: CropType):
        return self.calculate_ET0() * crop.Kc
    
    def calculate_ETa(self, crop: CropType, M_30: float):
        """Actual ET (stress-adjusted for soil moisture)"""
        ETc = self.calculate_ETc(crop)
        
        if M_30 >= crop.M_optimal:
            stress_factor = 1.0
        else:
            stress_factor = max(0, (M_30 - crop.M_min) / (crop.M_optimal - crop.M_min))
        
        return ETc * stress_factor


# ============================================================================
# CONTROL MODES
# ============================================================================

class ControlMode(Enum):
    PREDICTIVE = "predictive"
    REACTIVE = "reactive"
    FAULT = "fault"
    IDLE = "idle"


# ============================================================================
# PREDICTIVE CONTROLLER ENGINE
# ============================================================================

class PredictiveControlEngine:
    """
    Core logic for exponential decay prediction and optimal trigger calculation.
    
    Physics:
      M(t) = M_∞ + (M₀ - M_∞) * exp(-λ*t)
    
    Where:
      M₀ = current moisture
      M_∞ = asymptotic minimum (residual moisture baseline)
      λ = decay constant (derived from ETa)
      t = time (hours)
    """
    
    def __init__(self, crop: CropType, response_delay_minutes: float = 10.0):
        """
        Args:
            crop: CropType definition
            response_delay_minutes: System response latency (pump startup, wetting front, etc.)
        """
        self.crop = crop
        self.response_delay_hours = response_delay_minutes / 60.0
        self.M_residual = crop.M_min - 5  # Assume 5% safety buffer below minimum
    
    def estimate_decay_constant(self, ETa_mm_hr: float) -> float:
        """
        Estimate decay constant λ from ETa.
        
        ETa represents moisture loss rate. Convert to fraction of root zone depth per hour.
        
        λ = (ETa_mm_hr / root_depth_mm) * loss_fraction_per_mm
        
        Returns:
            λ (units: 1/hour)
        """
        if ETa_mm_hr <= 0:
            return 0.001  # Minimal decay if no ET
        
        # ETa as % of root zone lost per hour
        loss_per_hour = (ETa_mm_hr / self.crop.root_depth_mm) * 100.0
        
        # Convert to decay constant
        # Assume exponential decay: M = M∞ + (M₀ - M∞)*e^(-λ*t)
        # loss_per_hour ≈ λ * 100 (for small λ, loss is approximately linear)
        # More precisely: λ = -ln(1 - loss_per_hour/100) / dt
        
        if loss_per_hour >= 100:
            loss_per_hour = 99.9
        
        lambda_val = -math.log(1.0 - loss_per_hour / 100.0)
        return lambda_val
    
    def predict_moisture_at_time(self, M_current: float, lambda_val: float, t_hours: float) -> float:
        """
        Predict soil moisture at time t_hours in the future.
        
        Args:
            M_current: Current moisture (%)
            lambda_val: Decay constant (1/hour)
            t_hours: Time horizon (hours)
        
        Returns:
            Predicted moisture (%)
        """
        if lambda_val <= 0:
            return M_current
        
        M_predicted = self.M_residual + (M_current - self.M_residual) * math.exp(-lambda_val * t_hours)
        return max(self.crop.M_min - 1, M_predicted)  # Never predict below critical
    
    def time_to_threshold(self, M_current: float, lambda_val: float, M_threshold: float) -> float:
        """
        Calculate time (hours) until moisture crosses a threshold.
        
        Solves: M_threshold = M_residual + (M_current - M_residual) * exp(-λ*t)
        
        Args:
            M_current: Current moisture (%)
            lambda_val: Decay constant (1/hour)
            M_threshold: Target threshold (%)
        
        Returns:
            Time to reach threshold (hours). Returns 0 if already crossed, large value if never will.
        """
        if lambda_val <= 0:
            return float('inf')
        
        # Already at or below threshold
        if M_current <= M_threshold:
            return 0.0
        
        # Check if threshold is reachable
        if M_threshold < self.M_residual:
            return float('inf')
        
        # Solve exponential equation
        ratio = (M_threshold - self.M_residual) / (M_current - self.M_residual)
        
        if ratio <= 0:
            return float('inf')
        
        t_hours = -math.log(ratio) / lambda_val
        return max(0, t_hours)
    
    def calculate_optimal_trigger(self, M_current: float, lambda_val: float) -> dict:
        """
        Core algorithm: Calculate WHEN and AT WHAT THRESHOLD to start irrigation
        to maintain soil in optimal zone with minimal water use.
        
        Strategy:
        1. Target upper bound: M_optimal (stop irrigation here)
        2. Calculate trigger threshold: safety margin above minimum accounting for decay & response lag
        3. Return: trigger_moisture, hours_until_trigger
        
        Returns:
            {
                'trigger_moisture': threshold % to start irrigation,
                'hours_until_crossing': time until reaching trigger,
                'safety_margin': margin above M_min,
                'decay_rate_pct_per_hour': ETa-derived loss rate,
            }
        """
        
        # Moisture loss during response delay
        loss_during_response = self.predict_moisture_at_time(
            M_current, lambda_val, self.response_delay_hours
        )
        loss_amount = M_current - loss_during_response
        
        # Safety margin: account for response delay + small headroom
        safety_factor = 1.5  # 1.5x the expected loss during response delay
        safety_margin = loss_amount * safety_factor
        
        # Optimal trigger: start before you drop below optimal, but after you've used water
        trigger_moisture = self.crop.M_min + safety_margin
        
        # Ensure trigger is between min and optimal
        trigger_moisture = max(
            self.crop.M_min + 2,  # At least 2% above critical
            min(trigger_moisture, self.crop.M_optimal * 0.8)  # But not too high
        )
        
        # Time until we reach trigger
        t_trigger = self.time_to_threshold(M_current, lambda_val, trigger_moisture)
        
        # Decay rate as percentage per hour
        decay_rate = (loss_during_response - M_current) * -1 / max(1, self.response_delay_hours)
        
        return {
            'trigger_moisture': round(trigger_moisture, 1),
            'hours_until_crossing': round(t_trigger, 2),
            'safety_margin': round(safety_margin, 1),
            'decay_rate_pct_per_hour': round(decay_rate, 2),
            'M_residual_estimate': round(self.M_residual, 1),
        }


# ============================================================================
# IRRIGATION ZONE (REDESIGNED)
# ============================================================================

class IrrigationZone:
    """
    Per-zone predictive controller.
    Removes scheduled mode entirely. Uses exponential decay prediction.
    """
    
    def __init__(self, zone_id: str, crop: CropType, mqtt_client: MQTTClient):
        self.zone_id = zone_id
        self.crop = crop
        self.mqtt = mqtt_client
        
        # Telemetry
        self.M_10 = 60.0
        self.M_30 = 55.0
        self.M_60 = 50.0
        self.flow_rate = 0.0
        
        # Control state
        self.valve_state = False
        self.valve_on_time = 0.0
        self.max_valve_duration = 90  # minutes
        
        # Diagnostics
        self.active_mode = ControlMode.IDLE
        self.sensor_fault = False
        
        # Predictive controller
        self.predictor = PredictiveControlEngine(crop, response_delay_minutes=10.0)
        
        # Tracking for debugging
        self.last_trigger_info = {}
        self.last_eta = 0.0

    def update_from_mqtt(self):
        """Fetch latest telemetry from sensor_publisher.py via Mosquitto."""
        topic = f"farm/zone/{self.zone_id}/sensors"
        payload = self.mqtt.get_latest(topic)
        if payload:
            self.M_10 = payload.get("M_10", self.M_10)
            self.M_30 = payload.get("M_30", self.M_30)
            self.M_60 = payload.get("M_60", self.M_60)
            self.flow_rate = payload.get("flow_rate", self.flow_rate)

    def to_mqtt_payload(self, weather: WeatherStation) -> dict:
        """Export current state for logging/analysis."""
        return {
            "timestamp": datetime.now().isoformat(),
            "zone_id": self.zone_id,
            "crop": self.crop.name,
            "sensors": {
                "M_10": round(self.M_10, 1),
                "M_30": round(self.M_30, 1),
                "M_60": round(self.M_60, 1),
                "flow_rate_L_min": round(self.flow_rate, 2),
            },
            "control": {
                "valve_state": self.valve_state,
                "valve_on_time_min": round(self.valve_on_time, 1),
                "active_mode": self.active_mode.value,
            },
            "diagnostics": {
                "ETc_mm_hr": round(weather.calculate_ETc(self.crop), 3),
                "ETa_mm_hr": round(self.last_eta, 3),
                "sensor_fault": self.sensor_fault,
                "predictive_trigger_info": self.last_trigger_info,
            }
        }

    def update(self, dt_minutes: float, weather: WeatherStation):
        """Main control loop (no scheduled hour/minute parameters needed)."""
        
        # 1. Read telemetry
        self.update_from_mqtt()
        
        # 2. Sensor diagnostics
        self._detect_sensor_fault()
        if self.sensor_fault:
            self.valve_state = False
            self.active_mode = ControlMode.FAULT
            return
        
        # 3. Calculate ETa and decay constant
        ETa = weather.calculate_ETa(self.crop, self.M_30)
        self.last_eta = ETa
        lambda_val = self.predictor.estimate_decay_constant(ETa)
        
        # 4. Store prior valve state
        previous_valve_state = self.valve_state
        
        # 5. Predictive control logic
        self._predictive_control(lambda_val, ETa)
        
        # 6. Publish state change to MQTT
        if self.valve_state != previous_valve_state:
            command_str = "ON" if self.valve_state else "OFF"
            control_topic = f"farm/zone/{self.zone_id}/control"
            self.mqtt.publish(control_topic, {"state": command_str})
            print(f"[MQTT OUTBOUND] {self.zone_id} -> Valve '{command_str}'")
        
        # 7. Track valve duration
        if self.valve_state:
            self.valve_on_time += dt_minutes
        else:
            self.valve_on_time = 0.0

    def _predictive_control(self, lambda_val: float, ETa_mm_hr: float):
        """
        Pure predictive control logic (no scheduled triggers).
        
        Strategy:
        1. If M_30 is critically low → REACTIVE emergency
        2. Else if predicted to cross trigger threshold → PREDICTIVE early start
        3. If valve is ON and reached target → turn OFF
        """
        
        # =========================================================================
        # TURN-ON LOGIC
        # =========================================================================
        if not self.valve_state:
            
            # A. REACTIVE EMERGENCY (critical dry)
            if self.M_30 <= self.crop.M_min:
                self.valve_state = True
                self.active_mode = ControlMode.REACTIVE
                self.last_trigger_info = {
                    "trigger": "REACTIVE_CRITICAL",
                    "M_30": round(self.M_30, 1),
                    "reason": f"Below M_min ({self.crop.M_min}%)"
                }
                print(f"[{self.zone_id}] REACTIVE: CRITICAL DRY M_30={self.M_30:.1f}%")
                return
            
            # B. PREDICTIVE ANTICIPATORY (will hit threshold soon)
            trigger_info = self.predictor.calculate_optimal_trigger(self.M_30, lambda_val)
            trigger_moisture = trigger_info['trigger_moisture']
            hours_until = trigger_info['hours_until_crossing']
            
            # Start if crossing threshold within 2 hours
            if self.M_30 <= trigger_moisture or hours_until < 2.0:
                self.valve_state = True
                self.active_mode = ControlMode.PREDICTIVE
                self.last_trigger_info = {
                    "trigger": "PREDICTIVE_ANTICIPATORY",
                    "M_30_current": round(self.M_30, 1),
                    "trigger_threshold": trigger_moisture,
                    "hours_until_crossing": trigger_info['hours_until_crossing'],
                    "decay_rate_pct_per_hour": trigger_info['decay_rate_pct_per_hour'],
                    "safety_margin": trigger_info['safety_margin'],
                }
                print(f"[{self.zone_id}] PREDICTIVE: Starting irrigation in anticipation")
                print(f"              Current M_30={self.M_30:.1f}%, Trigger threshold={trigger_moisture}%")
                print(f"              ETa loss rate={trigger_info['decay_rate_pct_per_hour']:.2f}%/hr")
                return
        
        # =========================================================================
        # TURN-OFF LOGIC
        # =========================================================================
        if self.valve_state:
            
            # Check if reached target upper bound
            if self.M_30 >= self.crop.M_optimal:
                self.valve_state = False
                self.last_trigger_info = {
                    "trigger": "TARGET_REACHED",
                    "M_30": round(self.M_30, 1),
                    "M_optimal": self.crop.M_optimal
                }
                print(f"[{self.zone_id}] OFF: Target moisture reached (M_30={self.M_30:.1f}%)")
                return
            
            # Safety timeout (prevent runaway)
            if self.valve_on_time >= self.max_valve_duration:
                self.valve_state = False
                self.last_trigger_info = {
                    "trigger": "SAFETY_TIMEOUT",
                    "valve_on_time_min": round(self.valve_on_time, 1)
                }
                print(f"[{self.zone_id}] OFF: Safety timeout ({self.valve_on_time:.0f} min)")
                return
    
    def _detect_sensor_fault(self):
        """Out-of-bounds sensor diagnostic."""
        if self.M_30 < 0 or self.M_30 > 100 or self.M_60 < 0 or self.M_60 > 100:
            self.sensor_fault = True


# ============================================================================
# SIMULATION ENGINE
# ============================================================================

class IrrigationSimulation:
    
    def __init__(self, mqtt_client: MQTTClient):
        self.mqtt = mqtt_client
        self.weather = WeatherStation(mqtt_client)
        self.zones = [
            IrrigationZone("Zone_A", TOMATO, mqtt_client=mqtt_client),
            IrrigationZone("Zone_B", LETTUCE, mqtt_client=mqtt_client),
            IrrigationZone("Zone_C", OLIVE, mqtt_client=mqtt_client),
        ]
        self.dt_minutes = 15
        self.telemetry_log = []
    
    def run(self, duration_minutes: int = 0):
        print(f"\n{'='*100}")
        print(f"PREDICTIVE IRRIGATION CONTROLLER (Scheduled Mode Removed)")
        print(f"{'='*100}\n")
        
        start_time = time.time()
        
        try:
            while True:
                # 1. Update weather from MQTT
                self.weather.update_from_mqtt()
                
                # 2. Update all zones (no hour/minute parameters)
                for zone in self.zones:
                    zone.update(self.dt_minutes, self.weather)
                    self.telemetry_log.append(zone.to_mqtt_payload(self.weather))
                
                # 3. Print status
                now = datetime.now()
                print(f"\n--- Time {now.strftime('%H:%M:%S')} ---")
                print(f"Weather: I={self.weather.irradiance:.0f}W/m², T={self.weather.T_amb:.1f}°C, "
                      f"ET₀={self.weather.calculate_ET0():.4f}mm/hr")
                
                for zone in self.zones:
                    status = "ON" if zone.valve_state else "OFF"
                    eta = zone.last_eta
                    print(f"  {zone.zone_id} ({zone.crop.name}): "
                          f"M10={zone.M_10:.1f}%, M30={zone.M_30:.1f}%, M60={zone.M_60:.1f}%, "
                          f"ETa={eta:.4f}mm/hr, Valve={status} [{zone.active_mode.value}]")
                    
                    if zone.last_trigger_info:
                        trigger = zone.last_trigger_info.get('trigger', 'N/A')
                        print(f"    └─ Last trigger: {trigger}")
                
                # Sleep before next cycle
                time.sleep(2)
                
                # Check duration
                if duration_minutes > 0:
                    if (time.time() - start_time) / 60 >= duration_minutes:
                        break

        except KeyboardInterrupt:
            print("\nController stopped (Ctrl+C)")
    
    def export_telemetry(self, filename: str = "telemetry_predictive.json"):
        with open(filename, 'w') as f:
            json.dump(self.telemetry_log, f, indent=2)
        print(f"\nTelemetry exported to {filename}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Predictive Irrigation Controller (MQTT-based, no scheduled mode)")
    parser.add_argument("--broker", default="localhost", help="Mosquitto broker host")
    parser.add_argument("--port", type=int, default=1883, help="Mosquitto broker port")
    parser.add_argument("--duration", type=int, default=0, help="Duration in minutes (0=infinite)")
    
    args = parser.parse_args()
    
    print(f"[MQTT] Connecting to Mosquitto at {args.broker}:{args.port}")
    mqtt_client = RemoteMQTTClient(args.broker, args.port)
    
    try:
        controller = IrrigationSimulation(mqtt_client)
        controller.run(duration_minutes=args.duration)
        controller.export_telemetry()
    finally:
        mqtt_client.disconnect()
        print("\nCleanly shut down")
# IrrigationSimulator
Simulates real world data to compute the evapotranspiration factor (using the FAO Penman-Monteith equation) and determine the rate of decay of soil moisture for each zone (depending on the type of crop). Uses MQTTX to communicate with a reactive/proactive irrigation controller that optimizes soil moisture for each zone and minimizes water consumption.

## Predictive Controller

The predictive controller uses current environmental data to anticipate irrigation needs:

*   **Models moisture decay:** 
    $$M(t) = M_\infty + (M_0 - M_\infty) \cdot e^{-\lambda \cdot t}$$ 
    *(where λ ∝ ETa)*
*   **Proactive triggering:** Predicts when soil will hit the critical threshold.
*   **Anticipatory start:** Starts irrigation early (at ~45-50% for an optimal ~55%).
*   **Stable optimization:** Maintains smooth control, keeping moisture in the optimal zone ($\pm$ 5-10%).

### Key Tuning Parameters

| Parameter | Default | Meaning |
| :--- | :--- | :--- |
| `response_delay_minutes` | `10` | System latency (pump start + wetting time) |
| `safety_factor` | `1.5` | Buffer margin above critical minimum |
| `M_optimal` | `50%` | Target soil moisture (crop-specific) |

The reactive controller protects the system in case of a clogged or burst pipe (if the flow rate remains zero despite a command to open a valve / it reaches a huge value despite the command to close it). 

## Architecture

<img width="2720" height="1520" alt="irrigation_architecture" src="https://github.com/user-attachments/assets/8f81a8ac-b108-4887-a3af-fe3150444811" />

### Components

*   **Sensor Simulator:** Publishes weather (irradiance, temperature, humidity, wind) and soil moisture ($M_{10}$, $M_{30}$, $M_{60}$) via MQTT.
*   **Mosquitto:** Message broker handling sensor telemetry and control signals.
*   **Predictive Controller:** Calculates decay constant $\lambda$ from $ETa$, predicts soil trajectory, and triggers irrigation anticipatorily.
*   **Telegraf:** Collects MQTT data and forwards to InfluxDB.
*   **InfluxDB:** Time-series database storing all sensor and control data for analysis.

## Run Locally

Use Docker Compose to spin up the entire environment:

1. Open a terminal inside the project folder.
2. Start the services by running:
   ```bash
   docker compose up
   open http://localhost:8086
   ```
3. This will open influxdb. Navigate to the query tab to view weather and soil moisture data. 

## Results Demonstration

The dashboard below visualizes the system's performance, tracking the soil moisture at the **30cm root zone** (top three lines) alongside the corresponding **valve flow rates** for three distinct irrigation zones. 

By toggling the controller, we can observe the direct impact of the predictive algorithm on soil stability:

*   **Controller Deactivated (Unchecked Decay):** Without algorithmic intervention, soil moisture follows a natural exponential decay trajectory, rapidly dropping below the target threshold and settling into a critical, non-optimal state.
*   **Controller Activated (Predictive Stability):** Once engaged, the controller calculates the decay rate and triggers anticipatory irrigation (visible as flow rate spikes). This proactive response seamlessly catches the falling moisture levels and locks them into a stable, optimal band, preventing crop stress while minimizing excess water consumption.

<img width="1782" height="302" alt="Screenshot From 2026-09-05 06-41-16" src="https://github.com/user-attachments/assets/ee1874f2-59dd-4528-9e3f-d9ffc4d3d639" />


FROM python:3.9-slim

WORKDIR /irrigation_automation

RUN pip install paho-mqtt

COPY sensor_publisher.py .
COPY irrigation_controller.py .
"""Constants for the Sonoff BMT01 integration."""

DOMAIN = "sonoff_bmt01"

CONF_KEY = "key"

# Advertised BLE name is "ck_<eWeLink device id>"
LOCAL_NAME_PREFIX = "ck_"

# GATT characteristics (service 0xAAAA)
WRITE_UUID = "0000bbb0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000bbb1-0000-1000-8000-00805f9b34fb"
NOTIFY2_UUID = "0000bbb3-0000-1000-8000-00805f9b34fb"

# Timings (seconds)
AUTH_TIMEOUT = 15
TICKET_TIMEOUT = 10
RESPONSE_TIMEOUT = 5
BATTERY_INTERVAL = 30 * 60
BATTERY_RETRY = 60
RECONNECT_DELAYS = (5, 10, 30, 60)

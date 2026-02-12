# scripts/config.py

# Baseline PHY
SR = 48000
PROTOCOL_ID = 0

# Default channel stress profile (used in benches)
DROP_DATA = 0.25
DROP_ACK = 0.10
CORRUPT_DATA = 0.03
CORRUPT_ACK = 0.01

# Default ARQ tuning (chosen by fast-sim + confirmed by ggwave-smoke)
DEFAULT_MAX_PAYLOAD = 32
DEFAULT_TIMEOUT_S = 0.2         # for ggwave runner
DEFAULT_TIMEOUT_MS = 50         # for fast-sim
DEFAULT_MAX_RETRIES = 50
# Default protocol params
DEFAULT_WINDOW = 4
ACCOUNT_PHY_TIME = True


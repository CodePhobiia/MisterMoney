# V3 Offline Worker — Sprint 9

Async adjudication tier for high-stakes market predictions using GPT-5.4-pro (or GPT-5.4 fallback).

## Architecture

```
┌─────────────────────┐
│  Route Orchestrator │ ──escalates──> EscalationQueue (Redis)
└─────────────────────┘                      │
                                             │ (priority sorted set)
                                             ▼
                                    ┌────────────────┐
                                    │ OfflineWorker  │
                                    │ (poll loop)    │
                                    └────────────────┘
                                             │
                                             │ deep review
                                             ▼
                                    ┌────────────────┐
                                    │  GPT-5.4-pro   │
                                    │  (or GPT-5.4)  │
                                    └────────────────┘
                                             │
                                             ▼
                                    ┌────────────────┐
                                    │ SignalPublisher│──> DB + Redis
                                    └────────────────┘
```

## Components

### 1. EscalationQueue (`queue.py`)
- Redis sorted set for priority queue
- Priority based on: notional, dispute risk, model disagreement, time to resolution, uncertainty
- Deduplication: re-enqueue updates priority if higher
- Operations: enqueue, dequeue, peek, size, remove

### 2. OfflineWorker (`worker.py`)
- Polls queue every 60 seconds
- Rate limited: max 20 markets/hour
- Process:
  1. Dequeue highest-priority market
  2. Gather evidence + previous signals
  3. Call GPT-5.4-pro with deep reasoning
  4. Compare with existing estimates
  5. Publish new signal
  6. Notify on significant disagreement (Δp > 0.15)

### 3. WeeklyEvaluator (`weekly_eval.py`)
- Runs every Sunday at midnight UTC
- Analyzes resolved markets from past 7 days
- Calculates Brier scores by route
- Uses GPT-5.4-pro to identify:
  - Route-specific biases
  - Systematic failures
  - Calibration recommendations
- Generates calibration labels for retraining
- Sends summary report to Telegram

### 4. Entry Point (`main.py`)
- Connects to Postgres + Redis
- Initializes provider registry
- Starts worker loop + weekly evaluator
- Graceful shutdown on SIGINT

### 5. Systemd Service (`v3-offline.service`)
- Auto-restart on failure
- Depends on: postgresql.service, redis.service
- Working directory: `/home/ubuntu/.openclaw/workspace/MisterMoney`

## Integration Tests (`test_offline.py`)

✅ All tests passing:
1. Queue enqueue/dequeue
2. Priority ordering (highest first)
3. Deduplication (priority update)
4. Peek without removal
5. Worker.process_one (mock market)
6. WeeklyEvaluator.generate_calibration_labels
7. Weekly report formatting

## Stats

- **Lines of Code**: 1,769 (Python)
- **Files Created**: 7 Python files + 1 systemd service
- **Test Coverage**: 7 integration tests

## Usage

### Run Worker
```bash
python -m v3.offline.main
```

### Run Tests
```bash
python -m v3.offline.test_offline
```

### Install Service
```bash
sudo cp v3/v3-offline.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable v3-offline
sudo systemctl start v3-offline
```

### Monitor
```bash
sudo systemctl status v3-offline
sudo journalctl -u v3-offline -f
```

## Provider Notes

- **GPT-5.4-pro**: Not yet available via Codex OAuth
- **Fallback**: Uses GPT-5.4 until 5.4-pro access is granted
- **Health checks**: Provider registry validates all models on startup
- **Graceful degradation**: Worker logs error but continues if provider unavailable

## Future Enhancements

1. **Calibrator Retraining**: Use generated labels to retrain route calibrators
2. **Dynamic Priority**: Adjust priority based on real-time market movement
3. **Batch Processing**: Process multiple low-priority markets together
4. **A/B Testing**: Compare offline vs route estimates in shadow mode
5. **Notification Channels**: Expand beyond Telegram (Discord, Slack, email)

## Dependencies

- `redis.asyncio`: Redis queue
- `asyncpg`: Postgres
- `structlog`: Structured logging
- `pydantic`: Data validation

All existing V3 components remain unchanged.

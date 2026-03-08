"""
V3 Shadow Mode Entry Point

Usage:
    python -m v3.shadow.main

Runs the V3 signal pipeline on live markets in shadow mode.
Signals are logged but never affect V1 execution.
"""

import asyncio
import os
import signal
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
import structlog

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from v3.evidence.db import Database
from v3.evidence.graph import EvidenceGraph
from v3.providers.registry import ProviderRegistry
from v3.shadow.runner import ShadowRunner
from v3.shadow.reports import DailyReporter
from v3.shadow.metrics import BrierScoreTracker

log = structlog.get_logger()


# Shadow mode configuration
CONFIG = {
    "db_dsn": os.getenv("V3_DB_DSN", "postgresql://mmbot:mmbot_v3_2026@localhost/mistermoney_v3"),
    "market_limit": 50,  # Top N markets to evaluate per cycle
    "cycle_interval_seconds": 300,  # 5 minutes
    "log_dir": "data/v3/shadow",
    "daily_report_time": time(0, 0),  # Midnight UTC
}


class ShadowModeService:
    """Shadow mode service with graceful shutdown"""
    
    def __init__(self):
        self.db: Database | None = None
        self.registry: ProviderRegistry | None = None
        self.evidence_graph: EvidenceGraph | None = None
        self.runner: ShadowRunner | None = None
        self.reporter: DailyReporter | None = None
        self.shutdown_event = asyncio.Event()
    
    async def initialize(self) -> None:
        """Initialize all components"""
        log.info("shadow_mode_initializing")
        
        # 1. Connect to Postgres
        self.db = Database(CONFIG["db_dsn"])
        await self.db.connect()
        
        # Create v3_predictions table if not exists
        await self._create_predictions_table()
        
        # 2. Initialize provider registry
        self.registry = ProviderRegistry()
        await self.registry.initialize()
        
        # 3. Initialize evidence graph
        self.evidence_graph = EvidenceGraph(self.db)
        
        # 4. Create shadow runner
        self.runner = ShadowRunner(
            db=self.db,
            registry=self.registry,
            evidence_graph=self.evidence_graph,
            config=CONFIG
        )
        
        # 5. Create daily reporter
        metrics = BrierScoreTracker(self.db)
        self.reporter = DailyReporter(
            db=self.db,
            metrics=metrics,
            logger_dir=CONFIG["log_dir"]
        )
        
        log.info("shadow_mode_initialized")
    
    async def _create_predictions_table(self) -> None:
        """Create v3_predictions table if not exists"""
        if self.db is None or self.db.pool is None:
            return
        
        await self.db.pool.execute("""
            CREATE TABLE IF NOT EXISTS v3_predictions (
                id SERIAL PRIMARY KEY,
                condition_id TEXT NOT NULL,
                route TEXT NOT NULL,
                p_predicted FLOAT NOT NULL,
                predicted_at TIMESTAMPTZ NOT NULL,
                actual_outcome FLOAT,
                brier_score FLOAT,
                scored_at TIMESTAMPTZ
            )
        """)
        
        await self.db.pool.execute("""
            CREATE INDEX IF NOT EXISTS idx_v3_predictions_condition 
            ON v3_predictions(condition_id)
        """)
        
        log.info("predictions_table_ready")
    
    async def run(self) -> None:
        """Run shadow mode service"""
        await self.initialize()
        
        # Create tasks
        tasks = [
            asyncio.create_task(self._run_shadow_loop()),
            asyncio.create_task(self._run_daily_report_scheduler()),
            asyncio.create_task(self._wait_for_shutdown()),
        ]
        
        # Run initial evaluation cycle
        log.info("running_initial_evaluation_cycle")
        await self.runner.run_cycle()
        
        # Wait for shutdown
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # Cleanup
        await self.cleanup()
    
    async def _run_shadow_loop(self) -> None:
        """Run shadow evaluation loop"""
        try:
            await self.runner.run_loop(
                interval_seconds=CONFIG["cycle_interval_seconds"]
            )
        except asyncio.CancelledError:
            log.info("shadow_loop_cancelled")
    
    async def _run_daily_report_scheduler(self) -> None:
        """Schedule daily reports at midnight UTC"""
        log.info("daily_report_scheduler_started", 
                time=CONFIG["daily_report_time"].isoformat())
        
        while not self.shutdown_event.is_set():
            try:
                # Calculate time until next report
                now = datetime.utcnow()
                target_time = datetime.combine(
                    now.date() + timedelta(days=1),
                    CONFIG["daily_report_time"]
                )
                
                wait_seconds = (target_time - now).total_seconds()
                
                log.info("daily_report_scheduled",
                        next_report=target_time.isoformat(),
                        wait_seconds=wait_seconds)
                
                # Wait until target time
                try:
                    await asyncio.wait_for(
                        self.shutdown_event.wait(),
                        timeout=wait_seconds
                    )
                    # Shutdown event set
                    break
                except asyncio.TimeoutError:
                    # Time to send report
                    pass
                
                # Send report for yesterday
                yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                
                log.info("sending_daily_report", date=yesterday)
                
                success = await self.reporter.send_report(date=yesterday)
                
                if success:
                    log.info("daily_report_sent", date=yesterday)
                else:
                    log.error("daily_report_failed", date=yesterday)
            
            except asyncio.CancelledError:
                log.info("daily_report_scheduler_cancelled")
                break
            
            except Exception as e:
                log.error("daily_report_scheduler_error", error=str(e))
                # Wait 1 hour before retry
                await asyncio.sleep(3600)
    
    async def _wait_for_shutdown(self) -> None:
        """Wait for shutdown signal"""
        await self.shutdown_event.wait()
        log.info("shutdown_signal_received")
    
    def signal_handler(self, sig, frame) -> None:
        """Handle shutdown signals"""
        log.info("signal_received", signal=sig)
        self.shutdown_event.set()
    
    async def cleanup(self) -> None:
        """Cleanup resources"""
        log.info("shadow_mode_cleanup")
        
        if self.runner:
            await self.runner.close()
        
        if self.db:
            await self.db.close()
        
        log.info("shadow_mode_cleanup_complete")


async def main():
    """Main entry point"""
    # Configure structured logging
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer()
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
    
    log.info("shadow_mode_starting")
    
    service = ShadowModeService()
    
    # Register signal handlers
    signal.signal(signal.SIGINT, service.signal_handler)
    signal.signal(signal.SIGTERM, service.signal_handler)
    
    try:
        await service.run()
    except Exception as e:
        log.error("shadow_mode_fatal_error", error=str(e))
        sys.exit(1)
    
    log.info("shadow_mode_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())

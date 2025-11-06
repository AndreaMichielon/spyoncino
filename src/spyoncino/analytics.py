"""
Security System Analytics Module

Handles event logging, data storage, and timeline visualization
for security system monitoring and analysis.
"""

import json
import logging
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Generator
from dataclasses import dataclass
from enum import Enum
import io

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session, Session
from sqlalchemy.pool import QueuePool
from sqlalchemy.sql import func
from contextlib import contextmanager

# SQLAlchemy declarative base
Base = declarative_base()

class EventModel(Base):
    """SQLAlchemy ORM model for security events."""
    __tablename__ = 'events'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    message = Column(Text, nullable=False)
    event_metadata = Column(Text, nullable=True)
    severity = Column(String, default='info', nullable=False)
    created_at = Column(DateTime, default=func.current_timestamp(), nullable=False)
    
    def __repr__(self):
        return f"<Event(id={self.id}, type={self.event_type}, time={self.timestamp})>"

class EventType(Enum):
    """Event types for classification."""
    MOTION = "motion"
    PERSON = "person"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    ERROR = "error"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    STORAGE_WARNING = "storage_warning"

@dataclass
class SecurityEvent:
    """Represents a security system event."""
    timestamp: datetime
    event_type: EventType
    message: str
    metadata: Optional[Dict[str, Any]] = None
    severity: str = "info"  # info, warning, error

class EventLogger:
    """
    Professional event logging system for security analytics.
    
    Stores events in SQLite database using SQLAlchemy ORM.
    """
    
    def __init__(self, db_path: str = "security_events.db"):
        """
        Initialize the event logger with SQLAlchemy and connection pooling.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Create SQLAlchemy engine with optimized connection pooling
        db_url = f"sqlite:///{self.db_path}"
        self.engine = create_engine(
            db_url,
            echo=False,
            poolclass=QueuePool,
            pool_size=5,              # Keep 5 connections open
            max_overflow=10,          # Allow 10 additional connections if needed
            pool_pre_ping=True,       # Verify connections before using
            pool_recycle=3600,        # Recycle connections after 1 hour
            connect_args={
                'check_same_thread': False,  # Allow SQLite multi-threading
                'timeout': 30                # 30 second timeout for locks
            }
        )
        
        # Create scoped session factory for thread-safe operations
        session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False
        )
        self.SessionLocal = scoped_session(session_factory)
        
        # Initialize database schema
        self._init_database()
    
    def _init_database(self) -> None:
        """Initialize database schema using SQLAlchemy ORM."""
        try:
            # Create all tables defined in Base metadata
            Base.metadata.create_all(self.engine)
            self.logger.debug(f"Event database initialized: {self.db_path}")
            
        except Exception as e:
            self.logger.error(f"Database initialization failed: {e}")
            raise
    
    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """
        Context manager for database sessions with automatic cleanup.
        
        Yields:
            Session: SQLAlchemy session from the connection pool
        """
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            self.SessionLocal.remove()  # Remove session from scoped registry
    
    def log_event(self, event: SecurityEvent) -> None:
        """
        Log an event to the database using connection pooling.
        
        Args:
            event: SecurityEvent to log
        """
        try:
            with self.get_session() as session:
                metadata_json = json.dumps(event.metadata) if event.metadata else None
                
                # Create ORM model instance
                event_model = EventModel(
                    timestamp=event.timestamp,
                    event_type=event.event_type.value,
                    message=event.message,
                    event_metadata=metadata_json,
                    severity=event.severity
                )
                
                session.add(event_model)
                # Commit is handled by context manager
                
        except Exception as e:
            self.logger.error(f"Failed to log event: {e}")
    
    def get_events(
        self, 
        hours: int = 24, 
        event_types: Optional[List[EventType]] = None
    ) -> List[SecurityEvent]:
        """
        Retrieve events from the last N hours using connection pooling.
        
        Args:
            hours: Number of hours to look back
            event_types: Filter by specific event types
            
        Returns:
            List of SecurityEvent objects
        """
        try:
            with self.get_session() as session:
                since = datetime.now() - timedelta(hours=hours)
                
                # Build SQLAlchemy query
                query = session.query(EventModel).filter(EventModel.timestamp >= since)
                
                # Add event type filter if specified
                if event_types:
                    event_type_values = [et.value for et in event_types]
                    query = query.filter(EventModel.event_type.in_(event_type_values))
                
                # Order by timestamp
                query = query.order_by(EventModel.timestamp.asc())
                
                # Execute query and convert to SecurityEvent objects
                events = []
                for event_model in query.all():
                    metadata = json.loads(event_model.event_metadata) if event_model.event_metadata else None
                    
                    events.append(SecurityEvent(
                        timestamp=event_model.timestamp,
                        event_type=EventType(event_model.event_type),
                        message=event_model.message,
                        metadata=metadata,
                        severity=event_model.severity
                    ))
                
                return events
            
        except Exception as e:
            self.logger.error(f"Failed to retrieve events: {e}")
            return []
    
    def create_timeline_plot(self, hours: int = 24) -> bytes:
        """
        Create a simplified 3-panel timeline: system uptime, activity counts, and critical events.
        
        Args:
            hours: Number of hours to include in timeline
            
        Returns:
            PNG image data as bytes
        """
        # Events are already sorted by timestamp from database query
        events = self.get_events(hours=hours)
        
        # Create time range with adaptive resolution
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)
        
        if hours <= 6:
            interval_minutes = 5
        elif hours <= 24:
            interval_minutes = 15
        elif hours <= 72:
            interval_minutes = 60
        else:
            interval_minutes = 120
        
        time_points = []
        current = start_time
        while current <= end_time:
            time_points.append(current)
            current += timedelta(minutes=interval_minutes)
        
        # Initialize counters for each time window
        motion_counts = [0] * len(time_points)
        person_counts = [0] * len(time_points)
        
        # Panel 1: Calculate system uptime by tracking actual state transitions
        # Build complete state history from events
        state_changes = []  # List of (timestamp, is_operational)
        
        # Smart initial state detection:
        # If we have motion/person events, system must have been operational
        has_activity = any(e.event_type in [EventType.MOTION, EventType.PERSON] for e in events)
        
        # Start with intelligent guess based on activity
        system_running = has_activity  # If there's activity, system was running
        camera_connected = has_activity
        
        for event in events:
            if event.event_type == EventType.STARTUP:
                system_running = True
                camera_connected = True
                state_changes.append((event.timestamp, 1))
            elif event.event_type == EventType.SHUTDOWN:
                system_running = False
                camera_connected = False
                state_changes.append((event.timestamp, 0))
            elif event.event_type == EventType.DISCONNECT:
                camera_connected = False
                is_operational = 1 if (system_running and camera_connected) else 0
                state_changes.append((event.timestamp, is_operational))
            elif event.event_type == EventType.RECONNECT:
                camera_connected = True
                is_operational = 1 if (system_running and camera_connected) else 0
                state_changes.append((event.timestamp, is_operational))
        
        # Build operational status array
        if not state_changes:
            # No critical events - use activity to determine state
            system_operational = [1 if has_activity else 0] * len(time_points)
        else:
            # Sample the state at each time_point based on state transitions
            system_operational = []
            
            # Initial state before first state change
            current_state = 1 if has_activity else 0
            
            # Update initial state if there's a state change before start_time
            for ts, state in state_changes:
                if ts <= start_time:
                    current_state = state
                else:
                    break
            
            # Build array by replaying state changes
            change_idx = 0
            for time_point in time_points:
                # Apply all state changes up to this time_point
                while change_idx < len(state_changes) and state_changes[change_idx][0] <= time_point:
                    current_state = state_changes[change_idx][1]
                    change_idx += 1
                system_operational.append(current_state)
        
        # Panel 2: Count events in each time window (simple histogram)
        for event in events:
            # Find which time window this event belongs to
            for i in range(len(time_points) - 1):
                if time_points[i] <= event.timestamp < time_points[i + 1]:
                    if event.event_type == EventType.MOTION:
                        motion_counts[i] += 1
                    elif event.event_type == EventType.PERSON:
                        person_counts[i] += 1
                    break
            else:
                # Handle last time window
                if event.timestamp >= time_points[-1]:
                    if event.event_type == EventType.MOTION:
                        motion_counts[-1] += 1
                    elif event.event_type == EventType.PERSON:
                        person_counts[-1] += 1
        
        # Panel 3: Collect critical events
        critical_events = [
            e for e in events 
            if e.event_type in [
                EventType.STARTUP, EventType.SHUTDOWN, 
                EventType.DISCONNECT, EventType.RECONNECT,
                EventType.ERROR, EventType.STORAGE_WARNING
            ]
        ]
        
        # Calculate max count for y-axis (stacked bar height)
        combined_counts = [m + p for m, p in zip(motion_counts, person_counts)]
        max_count = max(combined_counts, default=0) if combined_counts else 1
        
        # Create single unified plot - compact height
        fig, ax = plt.subplots(figsize=(22, 5.5))
        fig.subplots_adjust(left=0.05, right=0.98, top=0.92, bottom=0.18)
        
        # Define colors
        colors = {
            'operational': '#4CAF50',  # Green
            'offline': '#EF5350',      # Red-ish
            'motion': '#2196F3',       # Blue
            'person': '#FF9800',       # Orange
            'startup': '#4CAF50',      # Green
            'shutdown': '#E91E63',     # Pink
            'disconnect': '#FF5722',   # Deep Orange
            'reconnect': '#009688',    # Teal
            'error': '#9C27B0',        # Purple
            'warning': '#FFC107'       # Amber
        }
        
        event_symbols = {
            EventType.STARTUP: '▲',
            EventType.SHUTDOWN: '▼',
            EventType.DISCONNECT: '⚠',
            EventType.RECONNECT: '●',
            EventType.ERROR: '✕',
            EventType.STORAGE_WARNING: '!'
        }
        
        event_colors_map = {
            EventType.STARTUP: colors['startup'],
            EventType.SHUTDOWN: colors['shutdown'],
            EventType.DISCONNECT: colors['disconnect'],
            EventType.RECONNECT: colors['reconnect'],
            EventType.ERROR: colors['error'],
            EventType.STORAGE_WARNING: colors['warning']
        }
        
        # ========================================
        # Single Unified Plot
        # ========================================
        
        # 1. Y-axis scale based on actual max count (with headroom for labels)
        if max_count > 0:
            y_max = max_count * 1.25  # 25% headroom for count labels
        else:
            y_max = 5
        
        # 2. Precise background coloring (use actual state transitions)
        if state_changes:
            # Start with initial state
            current_state = 1 if has_activity else 0
            for ts, state in state_changes:
                if ts <= start_time:
                    current_state = state
                else:
                    break
            
            # Draw each segment from state change to state change
            prev_time = start_time
            for ts, state in state_changes:
                if ts >= start_time:
                    # Draw segment with previous state
                    bg_color = colors['operational'] if current_state == 1 else colors['offline']
                    ax.axvspan(prev_time, ts, color=bg_color, alpha=0.15, zorder=1)
                    prev_time = ts
                    current_state = state
            
            # Final segment to end_time
            bg_color = colors['operational'] if current_state == 1 else colors['offline']
            ax.axvspan(prev_time, end_time, color=bg_color, alpha=0.15, zorder=1)
        else:
            # No state changes - single color throughout
            bg_color = colors['operational'] if has_activity else colors['offline']
            ax.axvspan(start_time, end_time, color=bg_color, alpha=0.15, zorder=1)
        
        # 3. Critical event lines (behind bars)
        for event in critical_events:
            color = event_colors_map.get(event.event_type, '#666')
            symbol = event_symbols.get(event.event_type, '●')
            
            ax.axvline(event.timestamp, color=color, linewidth=1.8, alpha=0.6, 
                      linestyle='-', zorder=2)
            ax.text(event.timestamp, 0, symbol, ha='center', va='bottom',
                   fontsize=11, color=color, weight='bold', zorder=10, clip_on=False,
                   bbox=dict(boxstyle='circle,pad=0.15', fc='white', ec=color, lw=2, alpha=0.95))
        
        # 4. Detection bars (in front of vlines)
        width = (time_points[1] - time_points[0]) * 0.6
        
        ax.bar(time_points, motion_counts, width=width, color=colors['motion'], 
              alpha=0.85, label=f'Motion ({sum(motion_counts)})', edgecolor='white', 
              linewidth=0.5, zorder=3)
        ax.bar(time_points, person_counts, width=width, bottom=motion_counts, 
              color=colors['person'], alpha=0.85, label=f'Person ({sum(person_counts)})',
              edgecolor='white', linewidth=0.5, zorder=3)
        
        # 5. Styling
        ax.set_ylabel('Events', fontsize=13, weight='bold')
        ax.set_xlim(start_time, end_time)
        ax.set_ylim(0, y_max)
        ax.grid(True, alpha=0.2, axis='both', linestyle='--', linewidth=0.7)
        ax.set_title(f'Security Analytics - Last {hours}h', fontsize=15, weight='bold', pad=10)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        
        # 6. Comprehensive legend with all symbols
        leg_items = [
            Patch(fc=colors['motion'], alpha=0.85, label='Motion'),
            Patch(fc=colors['person'], alpha=0.85, label='Person'),
            Patch(fc=colors['operational'], alpha=0.15, label='Online'),
            Patch(fc=colors['offline'], alpha=0.15, label='Offline')
        ]
        
        # Add all critical event types that appear in data
        if critical_events:
            event_types_present = {}
            for event in critical_events:
                if event.event_type not in event_types_present:
                    event_types_present[event.event_type] = event_colors_map.get(event.event_type, '#666')
            
            for event_type, color in event_types_present.items():
                symbol = event_symbols.get(event_type, '●')
                event_name = event_type.value.replace('_', ' ').title()
                leg_items.append(
                    Patch(fc=color, edgecolor='white', linewidth=2, 
                          label=f'{symbol} {event_name}')
                )
        
        ax.legend(handles=leg_items, loc='upper left', fontsize=9, framealpha=0.95, 
                 edgecolor='#ccc', ncol=min(6, len(leg_items)))
        
        # 7. X-axis
        if hours <= 6:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        elif hours <= 24:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        elif hours <= 168:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=12))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
        else:
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        
        ax.tick_params(axis='both', labelsize=10)
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha='center')
        
        # Add summary info if no events at all
        if not events:
            fig.text(0.5, 0.5, f'No events recorded in the last {hours} hours', 
                    ha='center', va='center', fontsize=14, style='italic', 
                    color='gray', transform=fig.transFigure, zorder=100)
        
        # Save to bytes
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png', dpi=150, bbox_inches='tight')
        buffer.seek(0)
        image_data = buffer.getvalue()
        plt.close(fig)
        
        return image_data
    
    def get_summary_stats(self, hours: int = 24) -> Dict[str, Any]:
        """
        Get summary statistics for the specified time period.
        
        Args:
            hours: Number of hours to analyze
            
        Returns:
            Dictionary with statistics
        """
        events = self.get_events(hours=hours)
        
        stats = {
            'total_events': len(events),
            'by_type': {},
            'by_severity': {},
            'time_period': f'{hours} hours',
            'first_event': None,
            'last_event': None
        }
        
        if events:
            stats['first_event'] = events[0].timestamp
            stats['last_event'] = events[-1].timestamp
            
            # Count by type
            for event in events:
                event_type = event.event_type.value
                stats['by_type'][event_type] = stats['by_type'].get(event_type, 0) + 1
                
                severity = event.severity
                stats['by_severity'][severity] = stats['by_severity'].get(severity, 0) + 1
        
        return stats
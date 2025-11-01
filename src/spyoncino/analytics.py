"""
Security System Analytics Module

Handles event logging, data storage, and timeline visualization
for security system monitoring and analysis.
"""

import sqlite3
import json
import logging
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass
from enum import Enum
import io
import base64

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
    
    Stores events in SQLite database and provides analysis capabilities.
    """
    
    def __init__(self, db_path: str = "security_events.db"):
        """
        Initialize the event logger.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._init_database()
    
    def _init_database(self) -> None:
        """Initialize SQLite database with events table."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME NOT NULL,
                        event_type TEXT NOT NULL,
                        message TEXT NOT NULL,
                        metadata TEXT,
                        severity TEXT DEFAULT 'info',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Create index for faster queries
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_timestamp 
                    ON events(timestamp)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_event_type 
                    ON events(event_type)
                """)
                
                conn.commit()
                self.logger.info(f"Event database initialized: {self.db_path}")
                
        except sqlite3.Error as e:
            self.logger.error(f"Database initialization failed: {e}")
            raise
    
    def log_event(self, event: SecurityEvent) -> None:
        """
        Log an event to the database.
        
        Args:
            event: SecurityEvent to log
        """
        try:
            metadata_json = json.dumps(event.metadata) if event.metadata else None
            
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO events (timestamp, event_type, message, metadata, severity)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    event.timestamp,
                    event.event_type.value,
                    event.message,
                    metadata_json,
                    event.severity
                ))
                conn.commit()
                
        except sqlite3.Error as e:
            self.logger.error(f"Failed to log event: {e}")
    
    def get_events(
        self, 
        hours: int = 24, 
        event_types: Optional[List[EventType]] = None
    ) -> List[SecurityEvent]:
        """
        Retrieve events from the last N hours.
        
        Args:
            hours: Number of hours to look back
            event_types: Filter by specific event types
            
        Returns:
            List of SecurityEvent objects
        """
        try:
            since = datetime.now() - timedelta(hours=hours)
            
            query = "SELECT timestamp, event_type, message, metadata, severity FROM events WHERE timestamp >= ?"
            params = [since]
            
            if event_types:
                placeholders = ','.join('?' * len(event_types))
                query += f" AND event_type IN ({placeholders})"
                params.extend([et.value for et in event_types])
            
            query += " ORDER BY timestamp ASC"
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(query, params)
                events = []
                
                for row in cursor.fetchall():
                    timestamp = datetime.fromisoformat(row[0])
                    event_type = EventType(row[1])
                    message = row[2]
                    metadata = json.loads(row[3]) if row[3] else None
                    severity = row[4]
                    
                    events.append(SecurityEvent(
                        timestamp=timestamp,
                        event_type=event_type,
                        message=message,
                        metadata=metadata,
                        severity=severity
                    ))
                
                return events
                
        except sqlite3.Error as e:
            self.logger.error(f"Failed to retrieve events: {e}")
            return []
    
    def create_timeline_plot(self, hours: int = 24) -> bytes:
        """
        Create a timeline plot with three subplots: system status, camera status, and activity detection.
        
        Args:
            hours: Number of hours to include in timeline
            
        Returns:
            PNG image data as bytes
        """
        events = self.get_events(hours=hours)
        
        # Create time range with adaptive resolution
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)
        
        if hours <= 6:
            interval_minutes = 2
        elif hours <= 24:
            interval_minutes = 10
        elif hours <= 72:
            interval_minutes = 30
        else:
            interval_minutes = 60
        
        time_points = []
        current = start_time
        while current <= end_time:
            time_points.append(current)
            current += timedelta(minutes=interval_minutes)
        
        # Initialize activity levels
        motion_activity = [0] * len(time_points)
        person_activity = [0] * len(time_points)
        
        # Process events chronologically for system and camera status
        events_sorted = sorted(events, key=lambda e: e.timestamp)
        
        # Initialize status
        system_status = [1] * len(time_points)  # 1 = on, 0 = off
        camera_status = [1] * len(time_points)  # 1 = connected, 0 = disconnected
        event_idx = 0
        system_running = True
        camera_connected = True
        
        for i, time_point in enumerate(time_points):
            # Process all events up to this time point
            while event_idx < len(events_sorted) and events_sorted[event_idx].timestamp <= time_point:
                event = events_sorted[event_idx]
                if event.event_type == EventType.STARTUP:
                    system_running = True
                    camera_connected = True
                elif event.event_type == EventType.SHUTDOWN:
                    system_running = False
                    camera_connected = False  # System off implies camera off
                elif event.event_type == EventType.DISCONNECT:
                    camera_connected = False
                elif event.event_type == EventType.RECONNECT:
                    camera_connected = True
                event_idx += 1
            
            # Set status values - system should reflect actual operational state
            system_status[i] = 1 if system_running and camera_connected else 0
            camera_status[i] = 1 if camera_connected else 0
        
        # Build activity curves with trend calculation
        motion_decay_rate = 0.95
        person_decay_rate = 0.90
        activity_boost = 20
        
        for i, time_point in enumerate(time_points):
            if i > 0:
                time_diff = (time_points[i] - time_points[i-1]).total_seconds() / 300
                motion_activity[i] = motion_activity[i-1] * (motion_decay_rate ** time_diff)
                person_activity[i] = person_activity[i-1] * (person_decay_rate ** time_diff)
            
            window_half = timedelta(minutes=interval_minutes/2)
            for event in events:
                if abs((event.timestamp - time_point).total_seconds()) <= window_half.total_seconds():
                    if event.event_type == EventType.MOTION:
                        motion_activity[i] = min(100, motion_activity[i] + activity_boost)
                    elif event.event_type == EventType.PERSON:
                        person_activity[i] = min(100, person_activity[i] + activity_boost)
        
        # Calculate dynamic y-axis limits for activity plot
        all_activity_values = motion_activity + person_activity
        max_activity = max(all_activity_values) if all_activity_values and any(x > 0 for x in all_activity_values) else 0
        
        if max_activity > 0:
            activity_y_max = max_activity * 1.15  # 15% headroom
        else:
            activity_y_max = 20  # Default range when no activity
        
        # Create figure with 3 subplots
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(16, 12), sharex=True)
        
        # Define colors
        colors = {
            'system': '#4CAF50',      # Green for system
            'camera': '#FF9800',      # Orange for camera
            'motion': '#2196F3',      # Blue for motion
            'person': '#F44336',      # Red for person
            
            # Event lines
            'startup': '#4CAF50',     # Green
            'shutdown': '#E91E63',    # Pink
            'disconnect': '#FF9800',  # Orange
            'reconnect': '#009688',   # Teal
            'error': '#9C27B0',       # Purple
            'warning': '#FFC107'      # Amber
        }
        
        # Event mapping
        event_mapping = {
            EventType.STARTUP: 'startup',
            EventType.SHUTDOWN: 'shutdown', 
            EventType.DISCONNECT: 'disconnect',
            EventType.RECONNECT: 'reconnect',
            EventType.ERROR: 'error',
            EventType.STORAGE_WARNING: 'warning'
        }
        
        event_labels = {
            EventType.STARTUP: 'START',
            EventType.SHUTDOWN: 'STOP',
            EventType.DISCONNECT: 'DISC',
            EventType.RECONNECT: 'CONN',
            EventType.ERROR: 'ERR',
            EventType.STORAGE_WARNING: 'WARN'
        }
        
        # Subplot 1: System Status (On/Off)
        ax1.step(time_points, system_status, color=colors['system'], linewidth=3, 
                where='post', alpha=0.8)
        ax1.fill_between(time_points, system_status, alpha=0.3, color=colors['system'], step='post')
        ax1.set_ylabel('System Status', fontsize=12)
        ax1.set_ylim(-0.1, 1.1)
        ax1.set_yticks([0, 1])
        ax1.set_yticklabels(['OFF', 'ON'])
        ax1.grid(True, alpha=0.3)
        ax1.set_title(f'Security System Timeline - Last {hours} Hours', fontsize=14, weight='bold')
        
        # Add event lines to system subplot
        for event in events:
            if event.event_type == EventType.STARTUP:
                ax1.axvline(x=event.timestamp, color=colors['startup'], linestyle='-', 
                        linewidth=2, alpha=0.8, zorder=3)
            elif event.event_type == EventType.SHUTDOWN:
                ax1.axvline(x=event.timestamp, color=colors['shutdown'], linestyle='-', 
                        linewidth=2, alpha=0.8, zorder=3)
            elif event.event_type in [EventType.ERROR, EventType.STORAGE_WARNING]:
                color_key = event_mapping[event.event_type]
                color = colors[color_key]
                ax1.axvline(x=event.timestamp, color=color, linestyle='-', 
                        linewidth=2, alpha=0.7, zorder=3)
        
        # Subplot 2: Camera Status (Connected/Disconnected)
        ax2.step(time_points, camera_status, color=colors['camera'], linewidth=3, 
                where='post', alpha=0.8)
        ax2.fill_between(time_points, camera_status, alpha=0.3, color=colors['camera'], step='post')
        ax2.set_ylabel('Camera Status', fontsize=12)
        ax2.set_ylim(-0.1, 1.1)
        ax2.set_yticks([0, 1])
        ax2.set_yticklabels(['DISCONNECTED', 'CONNECTED'])
        ax2.grid(True, alpha=0.3)
        
        # Add connection-related event lines to camera subplot
        for event in events:
            if event.event_type in [EventType.ERROR, EventType.STORAGE_WARNING]:
                color_key = event_mapping[event.event_type]
                color = colors[color_key]
                ax2.axvline(x=event.timestamp, color=color, linestyle='-', 
                        linewidth=2, alpha=0.7, zorder=3)
        
        # Subplot 3: Activity Detection (Motion & Person)
        has_motion = any(x > 0 for x in motion_activity)
        has_person = any(x > 0 for x in person_activity)
        
        if has_motion:
            ax3.plot(time_points, motion_activity, color=colors['motion'], linewidth=3, 
                    label='Motion Events', alpha=0.9, zorder=2)
            ax3.fill_between(time_points, motion_activity, alpha=0.3, 
                        color=colors['motion'], zorder=1)
        
        if has_person:
            ax3.plot(time_points, person_activity, color=colors['person'], linewidth=3, 
                    label='Person Events', alpha=0.9, zorder=2)
            ax3.fill_between(time_points, person_activity, alpha=0.3, 
                        color=colors['person'], zorder=1)
        
        ax3.set_ylabel('Activity Level', fontsize=12)
        ax3.set_xlabel('Time', fontsize=12)
        ax3.set_ylim(0, activity_y_max)
        ax3.grid(True, alpha=0.3)
        
        # Add all event lines to activity subplot
        for event in events:
            if event.event_type in [EventType.ERROR, EventType.STORAGE_WARNING]:
                color_key = event_mapping[event.event_type]
                color = colors[color_key]
                ax3.axvline(x=event.timestamp, color=color, linestyle='-', 
                        linewidth=2, alpha=0.7, zorder=3)
                
                # Add event labels only for warnings and errors
                label = event_labels[event.event_type]
                ax3.text(event.timestamp, activity_y_max * 0.95, label, 
                        rotation=90, ha='center', va='top', fontsize=9, 
                        color='white', weight='bold', zorder=5,
                        bbox=dict(boxstyle='round,pad=0.2', facecolor=color,
                        alpha=0.9, edgecolor='white', linewidth=1))
        
        # Add legend for activity plot
        if has_motion or has_person:
            ax3.legend(loc='upper left', fontsize=10, frameon=True, fancybox=True, shadow=True)
        
        # Smart x-axis formatting (only for bottom subplot since sharex=True)
        if hours <= 6:
            ax3.xaxis.set_major_locator(mdates.HourLocator(interval=1))
            ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        elif hours <= 24:
            ax3.xaxis.set_major_locator(mdates.HourLocator(interval=3))
            ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        elif hours <= 168:
            ax3.xaxis.set_major_locator(mdates.HourLocator(interval=12))
            ax3.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
        else:
            ax3.xaxis.set_major_locator(mdates.DayLocator(interval=1))
            ax3.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        
        plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45)
        
        # Create legend for event lines
        legend_elements = []
        legend_elements.append(plt.Line2D([0], [0], color='black', linewidth=0, label='Events:'))
        legend_elements.append(plt.Line2D([0], [0], color=colors['warning'], 
                                        linewidth=2, label='  Warning'))
        legend_elements.append(plt.Line2D([0], [0], color=colors['error'], 
                                        linewidth=2, label='  Error'))
        
        # Place legend on the right side
        fig.legend(handles=legend_elements, loc='lower center', bbox_to_anchor=(0.5, -0.05), 
            ncol=3, fontsize=10, frameon=True, fancybox=True, shadow=True)

        plt.tight_layout()
        plt.subplots_adjust(bottom=0.05)
        
        if not events:
            ax3.text(0.5, 0.5, f'No events in the last {hours} hours', 
                ha='center', va='center', transform=ax3.transAxes, fontsize=14)
        
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
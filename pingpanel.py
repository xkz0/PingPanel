import asyncio
import datetime
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Tuple
import requests
import yaml
from textual import log, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import (Button, Checkbox, Footer, Header, Input, Label,
                            RichLog, Sparkline, Static, Tree, SelectionList)
from textual.worker import Worker

class ConfigManager:
    CONFIG_FILE = "pingpanel_config.json"

    @staticmethod
    def save_config(
        inventory_path: str, 
        check_interval: int = 60,
        max_latency: int = 1000,
        ping_count: int = 3,
        max_threads: int = 10,
        tailscale_api_key: str = "",
        key_expiry_days: int = 90,
        tailnet_org: str = "",
        auto_ping_delay: int = 0,
        important_tailscale_tags: list = None
    ) -> None:
        key_expiry_date = None
        if tailscale_api_key:
            key_expiry_date = (datetime.datetime.now() + 
                              datetime.timedelta(days=key_expiry_days)).isoformat()
            
        with open(ConfigManager.CONFIG_FILE, "w") as f:
            json.dump({
                "inventory_path": inventory_path,
                "check_interval": check_interval,
                "max_latency": max_latency,
                "ping_count": ping_count,
                "max_threads": max_threads,
                "tailscale_api_key": tailscale_api_key,
                "key_expiry_date": key_expiry_date,
                "key_expiry_days": key_expiry_days,
                "tailnet_org": tailnet_org,
                "auto_ping_delay": auto_ping_delay,
                "important_tailscale_tags": important_tailscale_tags or ["server", "important"]
            }, f)
    
    @staticmethod
    def load_config() -> dict:
        if os.path.exists(ConfigManager.CONFIG_FILE):
            with open(ConfigManager.CONFIG_FILE, "r") as f:
                config = json.load(f)
                return {
                    "inventory_path": config.get("inventory_path", ""),
                    "check_interval": config.get("check_interval", 60),
                    "max_latency": config.get("max_latency", 1000),
                    "ping_count": config.get("ping_count", 3),
                    "max_threads": config.get("max_threads", 10),
                    "tailscale_api_key": config.get("tailscale_api_key", ""),
                    "key_expiry_date": config.get("key_expiry_date", None),
                    "key_expiry_days": config.get("key_expiry_days", 90),
                    "tailnet_org": config.get("tailnet_org", ""),
                    "auto_ping_delay": config.get("auto_ping_delay", 0),
                    "important_tailscale_tags": config.get("important_tailscale_tags", ["server", "important"])
                }
        return {
            "inventory_path": "", 
            "check_interval": 60,
            "max_latency": 1000,
            "ping_count": 3,
            "max_threads": 10,
            "tailscale_api_key": "",
            "key_expiry_date": None,
            "key_expiry_days": 90,
            "tailnet_org": "",
            "auto_ping_delay": 0,
            "important_tailscale_tags": ["server", "important"]
        }

    @staticmethod
    def days_until_key_expiry() -> int:
        config = ConfigManager.load_config()
        expiry_date = config.get("key_expiry_date")
        
        if not expiry_date:
            return None
            
        try:
            expiry = datetime.datetime.fromisoformat(expiry_date)
            now = datetime.datetime.now()
            days_left = (expiry - now).days
            return max(0, days_left)
        except (ValueError, TypeError):
            return None

class LogManager:
    CURRENT_LOG_FILE = "pingpanel_current_logs.json"
    AGGREGATED_LOG_FILE = "pingpanel_aggregated_logs.json"
    
    @staticmethod
    def save_log_entry(host: str, group: str, latency: float, timestamp: str, success: bool) -> None:
        log_entry = {
            "host": host,
            "group": group,
            "latency": latency,
            "timestamp": timestamp,
            "success": success
        }
        
        existing_logs = []
        if os.path.exists(LogManager.CURRENT_LOG_FILE):
            try:
                with open(LogManager.CURRENT_LOG_FILE, 'r') as f:
                    existing_logs = json.load(f)
            except json.JSONDecodeError:
                existing_logs = []
        
        # Keep only entries from the last 24 hours - we want to keep more history
        # for sparklines while still managing file size
        current_time = datetime.datetime.fromisoformat(timestamp)
        day_ago = current_time - datetime.timedelta(hours=24)
        
        # First filter by recency - only keep logs from last 24 hours
        recent_logs = [
            log for log in existing_logs
            if datetime.datetime.fromisoformat(log["timestamp"]) > day_ago
        ]
        
        # Add the new entry
        recent_logs.append(log_entry)
        
        # If we have too many logs, limit by count per host
        if len(recent_logs) > 1000:  # arbitrary limit to prevent file growth
            # Group by host and keep more entries per host (up to 100)
            logs_by_host = {}
            for log in recent_logs:
                log_host = log["host"]
                if log_host not in logs_by_host:
                    logs_by_host[log_host] = []
                logs_by_host[log_host].append(log)
            
            # Keep more entries per host
            logs_to_keep = []
            for host_logs in logs_by_host.values():
                # Sort by timestamp (newest first)
                sorted_logs = sorted(
                    host_logs,
                    key=lambda x: datetime.datetime.fromisoformat(x["timestamp"]),
                    reverse=True
                )
                # Keep up to 100 entries per host instead of just 5
                logs_to_keep.extend(sorted_logs[:100])
        else:
            logs_to_keep = recent_logs
        
        with open(LogManager.CURRENT_LOG_FILE, 'w') as f:
            json.dump(logs_to_keep, f, indent=2)
        
        LogManager.update_aggregated_logs(host, group, latency, timestamp, success)

    @staticmethod
    def update_aggregated_logs(host: str, group: str, latency: float, timestamp: str, success: bool) -> None:
        aggregated_logs = {}
        if os.path.exists(LogManager.AGGREGATED_LOG_FILE):
            try:
                with open(LogManager.AGGREGATED_LOG_FILE, 'r') as f:
                    aggregated_logs = {entry["host"]: entry for entry in json.load(f)}
            except json.JSONDecodeError:
                aggregated_logs = {}
        
        if host in aggregated_logs:
            entry = aggregated_logs[host]
            total_checks = entry["total_checks"] + 1
            if success:
                old_total = entry["avg_latency"] * entry["total_checks"]
                new_avg = (old_total + latency) / total_checks
                entry["avg_latency"] = new_avg
                entry["last_successful"] = timestamp
            
            successful_checks = (entry["uptime_percentage"] / 100 * entry["total_checks"])
            if success:
                successful_checks += 1
            entry["uptime_percentage"] = (successful_checks / total_checks) * 100
            entry["total_checks"] = total_checks
            entry["last_check"] = timestamp
            
        else:
            aggregated_logs[host] = {
                "host": host,
                "group": group,
                "avg_latency": latency if success else 0,
                "uptime_percentage": 100 if success else 0,
                "total_checks": 1,
                "last_check": timestamp,
                "last_successful": timestamp if success else None
            }
        
        with open(LogManager.AGGREGATED_LOG_FILE, 'w') as f:
            json.dump(list(aggregated_logs.values()), f, indent=2)

    @staticmethod
    def calculate_uptime(host: str, hours: int = 24) -> float:
        current_time = datetime.datetime.now()
        threshold_time = current_time - datetime.timedelta(hours=hours)
        
        current_logs = []
        if os.path.exists(LogManager.CURRENT_LOG_FILE):
            try:
                with open(LogManager.CURRENT_LOG_FILE, 'r') as f:
                    current_logs = json.load(f)
            except json.JSONDecodeError:
                current_logs = []

        aggregated_logs = []
        if os.path.exists(LogManager.AGGREGATED_LOG_FILE):
            try:
                with open(LogManager.AGGREGATED_LOG_FILE, 'r') as f:
                    aggregated_logs = json.load(f)
            except json.JSONDecodeError:
                aggregated_logs = []

        recent_logs = [
            log for log in current_logs
            if log['host'] == host and
            datetime.datetime.fromisoformat(log['timestamp']) > threshold_time
        ]
        
        host_aggregate = next(
            (log for log in aggregated_logs if log['host'] == host and
             datetime.datetime.fromisoformat(log['last_check']) > threshold_time),
            None
        )

        if not recent_logs and not host_aggregate:
            return 0.0

        total_checks = len(recent_logs)
        total_successes = sum(1 for log in recent_logs if log['success'])

        if host_aggregate:
            total_checks += host_aggregate['total_checks']
            total_successes += (host_aggregate['total_checks'] * host_aggregate['uptime_percentage'] / 100)

        return (total_successes / total_checks) * 100 if total_checks > 0 else 0.0

    @staticmethod
    def get_last_online(host: str) -> str:
        if os.path.exists(LogManager.AGGREGATED_LOG_FILE):
            try:
                with open(LogManager.AGGREGATED_LOG_FILE, 'r') as f:
                    logs = json.load(f)
                    for log in logs:
                        if log['host'] == host and log.get('last_successful'):
                            return datetime.datetime.fromisoformat(log['last_successful']).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        
        if os.path.exists(LogManager.CURRENT_LOG_FILE):
            try:
                with open(LogManager.CURRENT_LOG_FILE, 'r') as f:
                    logs = json.load(f)
                    successful_logs = [log for log in logs if log['host'] == host and log['success']]
                    if successful_logs:
                        latest = max(successful_logs, key=lambda x: x['timestamp'])
                        return datetime.datetime.fromisoformat(latest['timestamp']).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        return "Never"

    @staticmethod
    def get_latency_history(host: str, hours: int = 24, points: int = 360) -> list:
        """
        Fetch latency history for a specific host from aggregated logs
        Returns a list of latency values suitable for sparkline
        """
        try:
            all_logs = []
            current_time = datetime.datetime.now()
            threshold_time = current_time - datetime.timedelta(hours=hours)
            
            # Check current logs first
            if os.path.exists(LogManager.CURRENT_LOG_FILE):
                try:
                    with open(LogManager.CURRENT_LOG_FILE, 'r') as f:
                        current_logs = json.load(f)
                        # Get all matching logs within the time range
                        matching_logs = [
                            log for log in current_logs 
                            if log['host'] == host and 
                               log['success'] and
                               datetime.datetime.fromisoformat(log['timestamp']) >= threshold_time
                        ]
                        all_logs.extend(matching_logs)
                        log.debug(f"Found {len(matching_logs)} log entries for {host} in current logs")
                except json.JSONDecodeError:
                    log.error(f"Failed to decode current log file: {LogManager.CURRENT_LOG_FILE}")
                    pass
            
            # Sort by timestamp and extract latencies
            all_logs.sort(key=lambda x: x['timestamp'])
            latencies = [log['latency'] for log in all_logs if log['success']]
            
            # Only use aggregated logs as fallback if we have very few data points
            if len(latencies) < 5 and os.path.exists(LogManager.AGGREGATED_LOG_FILE):
                try:
                    log.debug(f"Not enough data points ({len(latencies)}), checking aggregated logs")
                    with open(LogManager.AGGREGATED_LOG_FILE, 'r') as f:
                        aggregated = json.load(f)
                        host_entry = next(
                            (entry for entry in aggregated if entry['host'] == host),
                            None
                        )
                        
                        if host_entry:
                            avg_latency = host_entry.get('avg_latency', 0)
                            if avg_latency > 0:
                                # Use the average to pad sparse data
                                log.debug(f"Using average latency {avg_latency} from aggregated logs")
                                if len(latencies) == 0:
                                    return [avg_latency] * min(5, points)
                                # If we have some data but not enough, add the average as padding
                                while len(latencies) < 5:
                                    latencies.append(avg_latency)
                except json.JSONDecodeError:
                    log.error(f"Failed to decode aggregated log file: {LogManager.AGGREGATED_LOG_FILE}")
                    pass
            
            log.debug(f"Total latency data points for {host}: {len(latencies)}")
            
            # If we have more points than our limit, sample them
            if len(latencies) > points:
                log.debug(f"Sampling {points} points from {len(latencies)} total points")
                step = len(latencies) // points
                return latencies[::step][:points]
            
            # If we don't have enough data, pad with the last value or zeros
            if len(latencies) < 2:
                log.debug(f"Not enough data points, returning: {latencies or [0]}")
                return latencies or [0]
            
            log.debug(f"Returning {len(latencies)} data points for sparkline")    
            return latencies
            
        except Exception as e:
            log.error(f"Error getting latency history: {str(e)}", exc_info=True)
            return [0]

class PingMonitor:
    @staticmethod
    async def ping(host: str, count: int = 3) -> Tuple[bool, float]:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        try:
            start_time = time.time()
            process = await asyncio.create_subprocess_exec(
                "ping", param, str(count), host,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=count * 2)
            end_time = time.time()
            
            output = stdout.decode()
            if platform.system().lower() == "windows":
                if "Average" in output:
                    avg_line = [line for line in output.split("\n") if "Average" in line][0]
                    latency = float(avg_line.split("=")[-1].strip("ms"))
                else:
                    latency = 0.0
            else:
                if "rtt" in output:
                    avg_line = output.split("\n")[-2]
                    latency = float(avg_line.split("/")[4])
                else:
                    latency = 0.0
            
            return process.returncode == 0, latency
        except (asyncio.TimeoutError, Exception) as e:
            log.error(f"Ping error for {host}: {str(e)}")
            return False, 0.0

class InventoryParser:
    @staticmethod
    def parse_inventory(file_path: str) -> Dict[str, List[Tuple[str, str]]]:
        with open(file_path, 'r') as f:
            inventory = yaml.safe_load(f)
        
        groups = {}
        def extract_hosts(data, parent=None):
            if isinstance(data, dict):
                if 'hosts' in data:
                    group_name = parent if parent else 'ungrouped'
                    if group_name not in groups:
                        groups[group_name] = []
                    for hostname, host_data in data['hosts'].items():
                        if 'ansible_host' in host_data:
                            groups[group_name].append((hostname, host_data['ansible_host']))
                
                for key, value in data.items():
                    if isinstance(value, dict):
                        extract_hosts(value, key)

        extract_hosts(inventory)
        return groups

class TableScreen(Screen):
    def __init__(self, groups: Dict[str, List[Tuple[str, str]]]):
        super().__init__()
        self.groups = groups
        self.host_nodes = {}
        self.host_states = {}
        self.auto_check = False
        self.check_task = None
        self.last_state_change = None
        self.total_up = 0
        self.total_down = 0
        config = ConfigManager.load_config()
        self.ping_semaphore = asyncio.Semaphore(config["max_threads"])
        self._counter_lock = asyncio.Lock()
        self.selected_host = None
        self.selected_hostname = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Status: Ready", id="status"),
            Container(
                Static("[green]Up: 0[/]", id="total_up"),
                Static("[red]Down: 0[/]", id="total_down"),
                id="totals"
            ),
            Container(
                Container(
                    Tree("Hosts", id="host_tree"),
                    id="tree-container"
                ),
                Container(
                    RichLog(
                        highlight=True,
                        markup=True,
                        max_lines=1000,
                        wrap=True,
                        auto_scroll=True,
                        id="ping-log"
                    ),
                    id="log-container"
                ),
                id="split-container"
            ),
            Container(
                Button("Check Now", id="check"),
                Button("Auto Check", id="auto_check", variant="default"),
                Button("Back", id="back"),
                id="buttons",
            ),
        )
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#host_tree")
        log_widget = self.query_one("#ping-log", RichLog)
        
        log_widget.clear()
        log_widget.write("[bold green]Host Status Monitor Log[/]", expand=True)
        log_widget.write("Status changes and ping results will be recorded here", expand=True)
        log_widget.write("─" * 50, expand=True)
        
        self.log_message("[blue]Log ready for ping events[/]")
        
        self.total_up = 0
        self.total_down = 0
        
        for group, hosts in self.groups.items():
            group_node = tree.root.add(group)
            for hostname, ip in hosts:
                label = f"{hostname} ({ip}) ⟳"
                host_node = group_node.add(label)
                self.host_nodes[(group, hostname)] = host_node
                self.host_states[(group, hostname)] = None

    def log_message(self, message: str) -> None:
        try:
            log_widget = self.query_one("#ping-log", RichLog)
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            log_widget.write(
                f"[dim]{timestamp}[/] {message}", 
                expand=True,
                scroll_end=True,
                animate=True
            )
        except Exception as e:
            log.error(f"Error writing to RichLog: {str(e)}")

    async def check_single_host(self, group: str, hostname: str, ip: str) -> None:
        async with self.ping_semaphore:
            host_node = self.host_nodes[(group, hostname)]
            status = self.query_one("#status")
            
            try:
                config = ConfigManager.load_config()
                status.update(f"Status: Checking {hostname}...")
                self.log_message(f"Checking [bold]{hostname}[/] ({ip})...")
                
                is_alive, latency = await PingMonitor.ping(ip, config["ping_count"])
                timestamp = datetime.datetime.now().isoformat()
                
                is_acceptable = is_alive and latency <= config["max_latency"]
                
                LogManager.save_log_entry(
                    host=hostname,
                    group=group,
                    latency=latency,
                    timestamp=timestamp,
                    success=is_acceptable
                )
                
                uptime = LogManager.calculate_uptime(hostname)
                status_text = f"{latency:.1f}ms" if is_alive else "timeout"
                
                prev_state = self.host_states.get((group, hostname))
                
                async with self._counter_lock:
                    if prev_state is None:
                        if is_acceptable:
                            self.total_up += 1
                        else:
                            self.total_down += 1
                    elif prev_state != is_acceptable:
                        if is_acceptable and self.total_down > 0:
                            self.total_up += 1
                            self.total_down -= 1
                        elif not is_acceptable and self.total_up > 0:
                            self.total_down += 1
                            self.total_up -= 1
                        change_text = f"{hostname} is now {'ONLINE' if is_acceptable else 'OFFLINE'}"
                        self.last_state_change = change_text
                        status.update(f"Status: {change_text}")
                        
                        # Log state change to RichLog
                        if is_acceptable:
                            self.log_message(f"Host '[bold]{hostname}[/]' [green]came online[/] ({status_text})")
                        else:
                            self.log_message(f"Host '[bold]{hostname}[/]' [red]went offline[/] (Last up: {LogManager.get_last_online(hostname)})")
                            
                    else:
                        status.update(f"Status: Checking {hostname}...")
                
                if is_acceptable:
                    status_icon = "✓"
                    icon_markup = f"[green]{status_icon}[/]"
                elif is_alive:
                    status_icon = "!"
                    icon_markup = f"[yellow]{status_icon}[/]"
                else:
                    status_icon = "✗"
                    icon_markup = f"[red]{status_icon}[/]"
                
                self.host_states[(group, hostname)] = is_acceptable
                last_seen = f" (Last up: {LogManager.get_last_online(hostname)})" if not is_acceptable else ""
                
                host_node.label = f"{hostname} ({ip}) {icon_markup} [{status_text}] [blue]{uptime:.1f}%[/]{last_seen}"
                
                self.query_one("#total_up").update(f"[green]Up: {self.total_up}[/]")
                self.query_one("#total_down").update(f"[red]Down: {self.total_down}[/]")
                
                # Log ping result in compact form
                if is_acceptable:
                    self.log_message(f"[green]✓[/] {hostname.ljust(20)} {ip.ljust(15)} [green]{latency:.1f}ms[/]")
                elif is_alive:
                    self.log_message(f"[yellow]![/] {hostname.ljust(20)} {ip.ljust(15)} [yellow]{latency:.1f}ms[/] (exceeds threshold)")
                else:
                    self.log_message(f"[red]✗[/] {hostname.ljust(20)} {ip.ljust(15)} [red]timeout[/]")
                    
            except Exception as e:
                error_msg = f"Error during check: {str(e)}"
                print(error_msg)
                status.update(f"Status: {error_msg}")
                self.log_message(f"[red]Error checking {hostname}: {str(e)}[/]")

    async def check_hosts(self) -> None:
        status = self.query_one("#status")
        status.update("Status: Starting host checks...")
        self.log_message("[blue]━━━━━━━━━━━━━━━ Starting Host Checks ━━━━━━━━━━━━━━━[/]")
        
        try:
            tasks = []
            for group, data in self.groups.items():
                for hostname, ip in data:
                    task = asyncio.create_task(self.check_single_host(group, hostname, ip))
                    tasks.append(task)
            
            await asyncio.gather(*tasks)
            
            if self.last_state_change:
                status.update(f"Status: {self.last_state_change}")
            else:
                status.update("Status: All checks complete")
                
            self.log_message("[blue]━━━━━━━━━━━━━━━ All Checks Complete ━━━━━━━━━━━━━━━[/]")
            summary = f"[green]✓ {self.total_up} online[/] • [red]✗ {self.total_down} offline[/] • [blue]{self.total_up + self.total_down} total[/]"
            self.log_message(summary)
            
        except Exception as e:
            error_msg = f"Error during check: {str(e)}"
            print(error_msg)
            status.update(f"Status: {error_msg}")
            self.log_message(f"[red]Error during check: {str(e)}[/]")

    async def auto_check_hosts(self) -> None:
        config = ConfigManager.load_config()
        interval = config["check_interval"]
        
        while self.auto_check:
            await self.check_hosts()
            await asyncio.sleep(interval)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.auto_check = False
            if self.check_task:
                self.check_task.cancel()
            self.app.pop_screen()
        elif event.button.id == "check":
            self.run_worker(self.check_hosts())
        elif event.button.id == "auto_check":
            self.auto_check = not self.auto_check
            if self.auto_check:
                event.button.variant = "success"
                event.button.label = "Auto Check (On)"
                self.check_task = self.run_worker(self.auto_check_hosts())
            else:
                event.button.variant = "default"
                event.button.label = "Auto Check"
                if self.check_task:
                    self.check_task.cancel()
    
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        try:
            node = event.node
            
            # Skip if this is a group node
            if node.parent == self.query_one("#host_tree").root:
                self.selected_hostname = None
                return
            
            # Extract the hostname from the node label
            label = node.label
            if " (" in label:
                hostname = label.split(" (")[0]
                self.selected_hostname = hostname
                self.log_message(f"Selected host: [bold]{hostname}[/]")
        except Exception as e:
            log.error(f"Error in node selection: {str(e)}")
            self.selected_hostname = None

class KeyExpiryWarningScreen(Screen):
    def __init__(self, days_left: int):
        super().__init__()
        self.days_left = days_left
        
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static("⚠️ Tailscale API Key Expiration Warning ⚠️", id="warning-title"),
            Static(f"Your Tailscale API key will expire in {self.days_left} days.", id="warning-message"),
            Static("Please update your API key in the Settings menu before it expires.", id="warning-advice"),
            Button("Continue", id="continue", variant="primary"),
            id="warning-container"
        )
        yield Footer()
        
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "continue":
            self.app.pop_screen()

class SettingsScreen(Screen):
    def compose(self) -> ComposeResult:
        config = ConfigManager.load_config()
        yield Header()
        yield Container(
            Static("Settings", classes="settings-title"),
            Container(
                # Left column
                Container(
                    Static("Connection Settings", classes="settings-section-title"),
                    Static("Inventory File Path:", id="inventory_label"),
                    Button(config["inventory_path"] or "Not Set", id="inventory_path"),
                    Static("Max Threads:", id="threads_label"),
                    Button(str(config["max_threads"]), id="max_threads"),
                    Static("Auto-Ping Delay (seconds):", id="auto_ping_delay_label"),
                    Button(str(config["auto_ping_delay"]), id="auto_ping_delay"),
                    id="left-settings"
                ),
                # Middle column
                Container(
                    Static("Ping Configuration", classes="settings-section-title"),
                    Static("Check Interval (seconds):", id="interval_label"),
                    Button(str(config["check_interval"]), id="interval"),
                    Static("Max Acceptable Latency (ms):", id="latency_label"),
                    Button(str(config["max_latency"]), id="max_latency"),
                    Static("Ping Packet Count:", id="count_label"),
                    Button(str(config["ping_count"]), id="ping_count"),
                    id="middle-settings"
                ),
                # Right column
                Container(
                    Static("Tailscale Integration", classes="settings-section-title"),
                    Static("Tailscale API Key:", id="tailscale_api_label"),
                    Button("*****" if config["tailscale_api_key"] else "Not Set", id="tailscale_api_key"),
                    Static("Tailnet Organization:", id="tailnet_org_label"),
                    Button(config["tailnet_org"] or "Not Set", id="tailnet_org"),
                    Static("API Key Expiry (days):", id="key_expiry_label"),
                    Button(str(config["key_expiry_days"]), id="key_expiry_days"),
                    id="right-settings"
                ),
                id="settings-grid"
            ),
            Container(
                Button("Save", id="save"),
                Button("Back", id="back"),
                id="settings-buttons",
            ),
            id="settings-container"
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id in ["interval", "max_latency", "ping_count", "inventory_path", 
                                "max_threads", "tailscale_api_key", "key_expiry_days", 
                                "tailnet_org", "auto_ping_delay"]:
            prompts = {
                "interval": "Enter check interval in seconds:",
                "max_latency": "Enter maximum acceptable latency in milliseconds:",
                "ping_count": "Enter number of ping packets to send:",
                "inventory_path": "Enter path to Ansible inventory file:",
                "max_threads": "Enter maximum number of concurrent pings:",
                "tailscale_api_key": "Enter your Tailscale API key:",
                "key_expiry_days": "Enter days until API key expires (default 90):",
                "tailnet_org": "Enter your Tailnet organization (e.g. example.com or example):",
                "auto_ping_delay": "Enter delay before auto-ping (seconds, 0 for immediate):"
            }
            
            if event.button.id == "tailscale_api_key":
                current_value = ConfigManager.load_config()["tailscale_api_key"]
            else:
                current_value = str(self.query_one(f"#{event.button.id}").label)
                if current_value == "Not Set":
                    current_value = ""
                
            self.app.push_screen(
                TextPrompt(
                    prompts[event.button.id],
                    current_value,
                    event.button.id
                )
            )
        elif event.button.id == "save":
            try:
                interval = int(str(self.query_one("#interval").label))
                max_latency = int(str(self.query_one("#max_latency").label))
                ping_count = int(str(self.query_one("#ping_count").label))
                max_threads = int(str(self.query_one("#max_threads").label))
                key_expiry_days = int(str(self.query_one("#key_expiry_days").label))
                auto_ping_delay = int(str(self.query_one("#auto_ping_delay").label))
                inventory_path = str(self.query_one("#inventory_path").label)
                
                tailnet_org_btn = self.query_one("#tailnet_org")
                tailnet_org = str(tailnet_org_btn.label)
                if tailnet_org == "Not Set":
                    tailnet_org = ""
                
                button_text = str(self.query_one("#tailscale_api_key").label) 
                if button_text == "*****":
                    tailscale_api_key = ConfigManager.load_config()["tailscale_api_key"]
                else:
                    tailscale_api_key = button_text
                
                if interval < 1 or max_latency < 1 or ping_count < 1 or max_threads < 1 or key_expiry_days < 1:
                    raise ValueError("All values must be positive")
                    
                if auto_ping_delay < 0:
                    raise ValueError("Auto ping delay cannot be negative")
                
                if inventory_path != "Not Set" and inventory_path.strip() and not os.path.exists(inventory_path):
                    self.notify(f"Warning: Inventory file '{inventory_path}' does not exist")
                
                ConfigManager.save_config(
                    inventory_path,
                    interval,
                    max_latency,
                    ping_count,
                    max_threads,
                    tailscale_api_key,
                    key_expiry_days,
                    tailnet_org,
                    auto_ping_delay
                )
                self.app.pop_screen()
            except ValueError as e:
                self.notify(str(e))

class TextPrompt(Screen):
    def __init__(self, question: str, default_value: str, setting_id: str, callback=None):
        super().__init__()
        self.question = question
        self.default_value = default_value
        self.setting_id = setting_id
        self.callback = callback

    def compose(self) -> ComposeResult:
        yield Static(self.question)
        yield Input(
            value=self.default_value, 
            id="input"
        )
        yield Button("OK", id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            value = self.query_one("#input").value
            
            # If we have a callback, use it
            if self.callback:
                self.app.pop_screen()
                self.callback(value)
                return
                
            # Otherwise use the default behavior
            settings_screen = self.app.screen_stack[-2]
            
            if self.setting_id == "tailscale_api_key":
                settings_screen.query_one(f"#{self.setting_id}").label = value if value else "Not Set"
            else:
                settings_screen.query_one(f"#{self.setting_id}").label = value
                
            self.app.pop_screen()

class MainMenu(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Container(
                Static(
                    """[bold blue]██████╗[bold green]██╗[bold yellow]███╗   ██╗[bold red] ██████╗ [bold magenta]██████╗  [bold cyan]█████╗ [bold white]███╗   ██╗[bold blue]███████╗[bold green]██╗
[bold blue]██╔══██╗[bold green]██║[bold yellow]████╗  ██║[bold red]██╔════╝ [bold magenta]██╔══██╗[bold cyan]██╔══██╗[bold white]████╗  ██║[bold blue]██╔════╝[bold green]██║
[bold blue]██████╔╝[bold green]██║[bold yellow]██╔██╗ ██║[bold red]██║  ███╗[bold magenta]██████╔╝[bold cyan]███████║[bold white]██╔██╗ ██║[bold blue]█████╗  [bold green]██║
[bold blue]██╔═══╝ [bold green]██║[bold yellow]██║╚██╗██║[bold red]██║   ██║[bold magenta]██╔═══╝ [bold cyan]██╔══██║[bold white]██║╚██╗██║[bold blue]██╔══╝  [bold green]██║
[bold blue]    ██║     [bold green]██║[bold yellow]██║ ╚████║[bold red]╚██████╔╝[bold magenta]██║     [bold cyan]██║  ██║[bold white]██║ ╚████║[bold blue]███████╗[bold green]███████╗
[bold blue]    ╚═╝     [bold green]╚═╝[bold yellow]╚═╝  ╚═══╝[bold red] ╚═════╝ [bold magenta]╚═╝     [bold cyan]╚═╝  ╚═╝[bold white]╚═╝  ╚═══╝[bold blue]╚══════╝[bold green]╚══════╝[/]""", id="title-message"),
                Static("Courtesy of CrabManStan", id="courtesy-message"),
                Static("", classes="spacer"),
                Container(
                    Button("Start Monitoring", id="start_monitoring", classes="btn-sunset"),
                    Button("Settings", id="settings", classes="btn-lime"),
                    Button("Exit", id="exit", classes="btn-coral"),
                    classes="button-row"
                ),
                id="menu-container"
            ),
            id="main-container"
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "exit":
            self.app.exit()
        elif event.button.id == "settings":
            self.app.push_screen(SettingsScreen())
        elif event.button.id == "start_monitoring":
            self.app.push_screen(MonitoringTypeScreen())

class MonitoringTypeScreen(Screen):
    """Screen to select between Standard Ping or Tailscale monitoring."""
    
    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Container(
                Static("Select Monitoring Type", id="type-selection-title"),
                Static("", classes="spacer"),
                Container(
                    Button("Standard Ping", id="standard_ping", variant="primary", classes="btn-violet"),
                    Button("Tailscale", id="tailscale", variant="warning", classes="btn-ocean"),
                    Button("Back", id="back", classes="btn-coral"),
                    classes="button-row"
                ),
                id="type-selection-container"
            ),
            id="main-container"
        )
        yield Footer()
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "standard_ping":
            try:
                print("Loading config...")
                config = ConfigManager.load_config()
                inventory_path = config["inventory_path"]
                
                if not inventory_path or not os.path.exists(inventory_path):
                    self.notify(
                        "No inventory file set or file doesn't exist.\n"
                        "Please configure an inventory file in Settings first.",
                        title="Missing Inventory",
                        severity="error"
                    )
                    return
                    
                print("Parsing inventory...")
                groups = InventoryParser.parse_inventory(inventory_path)
                print(f"Found {len(groups)} groups")
                self.app.push_screen(TableScreen(groups))
            except Exception as e:
                print(f"Error starting monitoring: {e}")
        elif event.button.id == "tailscale":
            self.app.push_screen(TailscaleIntegrationScreen())

class PingPanel(App):
    BINDINGS = [
        Binding("d", "toggle_dark", "Toggle dark mode"),
    ]
    
    ENABLE_CONSOLE = True
    
    CSS = """
    Screen {
        border: none;
    }
    
    Tree {
        height: 1fr;
        min-height: 10;
        margin: 1 0;
    }
    
    Container {
        height: auto;
        overflow-x: auto;
    }
    
    #buttons {
        width: 100%;
        height: 3;
        dock: bottom;
        layout: horizontal;
        align: center middle;
        background: $surface;
        border-top: solid $primary;
    }
    
    Button {
        margin: 0 1;
        min-width: 16;
    }
    
    /* Jazzy Button Colors */
    .btn-sunset {
        background: #FF5E62;
        color: $text;
        border: tall $background;
    }
    
    .btn-sunset:hover {
        background: #FF9966;
    }
    
    .btn-ocean {
        background: #2C3E50;
        color: $text;
        border: tall $background;
    }
    
    .btn-ocean:hover {
        background: #4CA1AF;
    }
    
    .btn-violet {
        background: #8E2DE2;
        color: $text;
        border: tall $background;
    }
    
    .btn-violet:hover {
        background: #9F44D3;
    }
    
    .btn-lime {
        background: #00b09b;
        color: $text;
        border: tall $background;
    }
    
    .btn-lime:hover {
        background: #96c93d;
    }
    
    .btn-coral {
        background: #ff7e5f;
        color: $text;
        border: tall $background;
    }
    
    .btn-coral:hover {
        background: #feb47b;
    }
    
    .btn-cosmic {
        background: #614385;
        color: $text;
        border: tall $background;
    }
    
    .btn-cosmic:hover {
        background: #516395;
    }
    
    .btn-bubblegum {
        background: #FC466B;
        color: $text;
        border: tall $background;
    }
    
    .btn-bubblegum:hover {
        background: #F56991;
    }

    #main-container {
        align: center middle;
        height: 100%;
        width: 100%;
    }

    #menu-container {
        width: 100%;
        height: auto;
        align: center middle;
        padding: 1;
    }

    .button-row {
        layout: horizontal;
        align: center middle;
        height: auto;
        width: 100%;
        content-align: center middle;
    }

    #title-message {
        text-align: center;
        margin-bottom: 0;
        width: 100%;
    }

    #courtesy-message {
        text-align: center;
        margin-bottom: 1;
        width: 100%;
        color: $text-disabled;
    }

    .spacer {
        height: 1;
    }
    
    #type-selection-title {
        text-align: center;
        margin-bottom: 1;
        width: 100%;
        text-style: bold;
        color: $secondary;
    }

    #type-selection-container {
        width: 80%;
        height: auto;
        align: center middle;
        padding: 1;
        border: solid $primary;
    }

    #warning-container {
        width: 100%;
        height: auto;
        align: center middle;
        padding: 2;
        layout: vertical;
    }
    
    #warning-title {
        color: $warning;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    
    #warning-message {
        text-align: center;
        margin-bottom: 1;
    }
    
    #warning-advice {
        text-align: center;
        margin-bottom: 2;
    }
    
    #split-container {
        layout: horizontal;
        height: 1fr;
        width: 100%;
    }
    
    /* Standard monitoring screen tree container */
    #tree-container {
        width: 50%;
        height: 100%;
        border-right: solid $primary;
        padding: 0 1 0 0;
        overflow-y: auto;
        background: $surface-darken-1;
    }
    
    /* Standard monitoring screen log container */
    TableScreen #log-container {
        width: 50%;
        height: 100%;
        padding: 0 0 0 1;
        overflow-y: auto;
    }
    
    /* Tailscale right container layout */
    TailscaleIntegrationScreen #right-container {
        width: 50%;
        height: 100%;
        padding: 0 0 0 1;
        layout: vertical;
    }
    
    /* Tailscale log container */
    TailscaleIntegrationScreen #log-container {
        width: 100%;
        height: 90%;
        min-height: 10;
        border-bottom: solid $primary-darken-1;
        overflow-y: auto;
    }
    
    /* Tailscale sparkline container */
    TailscaleIntegrationScreen #sparkline-container {
        width: 100%;
        height: 10%;
        min-height: 6;
        padding: 1;
        margin-top: 1;
    }
    
    #tailnet_tree {
        height: 100%;
        min-height: 10;
        margin: 0;
        border: solid $primary;
        padding: 1;
        width: 100%;
        overflow-y: auto;
    }
    
    #tailscale-log {
        height: 100%;
        border: solid $primary;
        padding: 1;
        background: $surface-darken-1;
        overflow-y: auto;
    }
    
    #device-sparkline {
        width: 100%;
        height: 6;
        margin-top: 1;
    }
    
    .sparkline--device {
        height: 6;
        width: 100%;
    }
    
    .sparkline--device .sparkline--min-color {
        color: $success;
    }
    
    .sparkline--device .sparkline--max-color {
        color: $warning;
    }
    
    #sparkline-title {
        text-align: center;
        height: auto;
        margin-bottom: 1;
    }
    
    #tailscale-container {
        height: auto;
        width: 100%;
        padding: 1;
        layout: vertical;
    }
    
    #tailscale-title {
        text-align: center;
        margin-bottom: 1;
    }
    
    #tailscale-status {
        text-align: center;
        margin-bottom: 1;
        height: 1;
    }
    
    #tag-selection {
        width: 100%;
        height: 1fr;
        min-height: 10;
        margin: 1 0;
        background: $surface-darken-1;
    }
    
    #tags-title {
        text-align: center;
        margin-bottom: 0;
        width: 100%;
        text-style: bold;
    }
    
    #tags-description {
        text-align: center;
        margin-bottom: 1;
        width: 100%;
        color: $text-muted;
    }
    
    #selection-container {
        height: 1fr;
        min-height: 10;
        margin: 1 0;
        border: solid $primary;
        padding: 1;
    }

    /* Settings grid layout styles */
    #settings-container {
        padding: 1;
        height: auto;
        width: 100%;
    }

    .settings-title {
        text-align: center;
        margin-bottom: 1;
        text-style: bold;
        width: 100%;
    }

    .settings-section-title {
        text-align: center;
        margin-bottom: 1;
        background: $surface-darken-1;
        text-style: bold;
        width: 100%;
    }

    #settings-grid {
        layout: horizontal;
        height: auto;
        margin-bottom: 1;
        align: center top;
        width: 100%;
    }

    #left-settings, #middle-settings, #right-settings {
        width: 1fr;
        height: auto;
        margin: 0 1;
        border: solid $primary-darken-1;
        padding: 1;
        background: $surface-darken-2;
    }

    #settings-buttons {
        width: 100%;
        height: 3;
        layout: horizontal;
        align: center middle;
        margin-top: 1;
    }
    
    Static {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield MainMenu()
        yield Footer()

    def on_mount(self) -> None:
        log.info("Application starting")
        # Add jazzy colors to all buttons
        self._apply_jazzy_button_colors()
        for widget in self.query("*"):
            if hasattr(widget, 'can_focus'):
                widget.can_focus = False

        days_until_expiry = ConfigManager.days_until_key_expiry()
        if days_until_expiry is not None and days_until_expiry <= 14:
            log.warning(f"API key expiring in {days_until_expiry} days")
            self.push_screen(KeyExpiryWarningScreen(days_until_expiry))

        self.push_screen(MainMenu())
        
        log.info("Application initialized")
        
    def _apply_jazzy_button_colors(self) -> None:
        """Apply jazzy colors to all buttons in the application"""
        try:
            color_classes = ["btn-sunset", "btn-ocean", "btn-violet", "btn-lime", 
                            "btn-coral", "btn-cosmic", "btn-bubblegum"]
            
            log.debug("Applying jazzy button colors")
            
            # Add a delay to ensure all screens have been properly mounted
            def add_colors():
                for i, button in enumerate(self.query(Button)):
                    color_class = color_classes[i % len(color_classes)]
                    button.add_class(color_class)
                    log.debug(f"Added {color_class} to button {button.id}")
            
            # Use call_later to let the UI render first
            self.call_later(add_colors)
            
        except Exception as e:
            log.error(f"Error applying jazzy button colors: {str(e)}")
            
    def action_toggle_debug(self) -> None:
        self.console.toggle()
        log.debug("Debug console toggled")

class TailscaleAPI:
    YAML_FILE = "tailscale_devices.yaml"
    
    @staticmethod
    async def fetch_devices(api_key: str, tailnet_org: str) -> Dict:
        try:
            log.debug(f"Starting Tailscale API fetch for org: {tailnet_org}")
            
            with tempfile.NamedTemporaryFile(mode='w+', suffix='.json', delete=False) as temp_file:
                output_file = temp_file.name
            
            curl_cmd = [
                "curl", "-s",
                "--connect-timeout", "5",
                "--max-time", "30",
                "--retry", "3",
                "--retry-delay", "2",
                "--header", f"Authorization: Bearer {api_key}",
                f"https://api.tailscale.com/api/v2/tailnet/{tailnet_org}/devices"
            ]
            
            log.debug(f"Executing curl command to Tailscale API (with redacted API key)")
            process = await asyncio.create_subprocess_exec(
                *curl_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                error_msg = stderr.decode().strip()
                log.error(f"Curl command failed with code {process.returncode}: {error_msg}")
                return {"error": f"Curl command failed: {error_msg}"}
            
            raw_content = stdout.decode()
            log.debug(f"Raw content length: {len(raw_content)}")
            try:
                response = json.loads(raw_content)
                log.debug(f"Received JSON response with keys: {list(response.keys())}")
                
                with open("tailscale_raw_response.json", "w") as f:
                    f.write(raw_content)
                    log.debug("Saved raw JSON response for debugging")
            except json.JSONDecodeError as e:
                log.error(f"Failed to parse JSON: {str(e)}")
                with open("tailscale_failed_response.txt", "w") as f:
                    f.write(raw_content)
                    log.debug("Saved failed JSON response content for inspection")
                return {"error": "Invalid JSON response from Tailscale API"}
            
            with open(TailscaleAPI.YAML_FILE, 'w') as yaml_file:
                yaml.dump(response, yaml_file, default_flow_style=False)
            log.debug(f"Successfully saved Tailscale data to {TailscaleAPI.YAML_FILE}")
            return {"success": True, "file": TailscaleAPI.YAML_FILE, "devices": response.get('devices', [])}
        except Exception as e:
            log.error(f"Exception in fetch_devices: {str(e)}")
            return {"error": str(e)}
            
    @staticmethod
    def load_devices() -> Dict:
        try:
            log.debug(f"Loading Tailscale devices from {TailscaleAPI.YAML_FILE}")
            if not os.path.exists(TailscaleAPI.YAML_FILE):
                log.warning("Tailscale data file not found")
                return {"error": "No Tailscale data available. Please fetch data first."}
                
            with open(TailscaleAPI.YAML_FILE, 'r') as yaml_file:
                data = yaml.safe_load(yaml_file)
                
            log.debug(f"Loaded device data with {len(data.get('devices', []))} devices")
            return data
        except Exception as e:
            log.error(f"Error loading Tailscale devices: {str(e)}")
            return {"error": f"Failed to load Tailscale data: {str(e)}"}

class TailscaleIntegrationScreen(Screen):
    def __init__(self):
        super().__init__()
        self.device_nodes = {}
        self.group_nodes = {}
        self.device_states = {}
        self.previous_device_states = {}
        self.refresh_timer = None
        self.countdown_timer = None
        self.refresh_interval = 60
        self.last_refresh_time = None
        self.countdown_seconds = 0
        self.previous_online_count = 0
        self.previous_offline_count = 0
        self.first_refresh = True
        self.selected_device = None
        self.selected_device_name = None
        self.ping_in_progress = False
        self.ping_semaphore = None
        self.device_latencies = {}
        self.auto_ping = True
        self.auto_ping_delay_timer = None
                
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static("Tailscale Integration", id="tailscale-title"),
            Static("Ready to fetch Tailscale device data", id="tailscale-status"),
            Container(
                Container(
                    Tree("Tailscale Devices", id="tailnet_tree"),
                    id="tree-container"
                ),
                Container(
                    Container(
                        RichLog(
                            highlight=True,
                            markup=True,
                            max_lines=1000,
                            wrap=True,
                            auto_scroll=True,
                            id="tailscale-log"
                        ),
                        id="log-container"
                    ),
                    Container(
                        Static("Select a device to view latency history", id="sparkline-title"),
                        Sparkline(
                            data=[],
                            summary_function=max,
                            id="device-sparkline",
                            classes="sparkline--device",
                        ),
                        id="sparkline-container"
                    ),
                    id="right-container"
                ),
                id="split-container"
            ),
            Container(
                Checkbox("Auto-Refresh (60s)", value=True, id="auto_refresh"),
                Checkbox("Auto-Ping with Refresh", value=True, id="auto_ping"),
                Button("Get Tailnet Data", id="get_tailnet", variant="primary", classes="btn-sunset"),
                Button("Ping Devices", id="ping_devices", variant="primary", classes="btn-violet"),
                Button("Important Tags", id="important_tags", variant="warning", classes="btn-lime"),
                Button("Back", id="back", classes="btn-coral"),
                id="buttons",
            )
        )
        yield Footer()

    def log_message(self, message: str) -> None:
        try:
            log_widget = self.query_one("#tailscale-log", RichLog)
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            log_widget.write(
                f"[dim]{timestamp}[/] {message}", 
                expand=True,
                scroll_end=True,
                animate=True
            )
        except Exception as e:
            log.error(f"Error writing to RichLog: {str(e)}")

    def on_mount(self) -> None:
        log_widget = self.query_one("#tailscale-log", RichLog)
        
        log_widget.clear()
        log_widget.write(
            "[bold green]Tailscale Device Status Log[/]", 
            expand=True
        )
        log_widget.write(
            "Status changes and ping results will be recorded here", 
            expand=True
        )
        log_widget.write("─" * 50, expand=True)
        
        self.log_message("[blue]Log ready for Tailscale events[/]")
        
        config = ConfigManager.load_config()
        self.ping_semaphore = asyncio.Semaphore(config["max_threads"])
        self.auto_ping = self.query_one("#auto_ping").value
        
        if self.query_one("#auto_refresh").value:
            log.debug("Auto-refresh enabled on mount, starting initial data fetch")
            self._start_auto_refresh_timer()
            self._initial_fetch()
            
    def _initial_fetch(self) -> None:
        log.debug("Performing initial data fetch")
        self.fetch_tailscale_data()
        
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self._cancel_auto_refresh_timer()
            self.app.pop_screen()
        elif event.button.id == "get_tailnet":
            self.fetch_tailscale_data()
        elif event.button.id == "ping_devices":
            if not self.ping_in_progress:
                self.ping_tailscale_devices()
            else:
                self.notify("Ping already in progress")
        elif event.button.id == "important_tags":
            self._cancel_auto_refresh_timer()  # Pause auto-refresh while in settings
            self.app.push_screen(TailscaleTagSettingsScreen())
    
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "auto_refresh":
            if event.checkbox.value:
                self._start_auto_refresh_timer()
                self.fetch_tailscale_data()
            else:
                self._cancel_auto_refresh_timer()
        elif event.checkbox.id == "auto_ping":
            self.auto_ping = event.checkbox.value
            log.debug(f"Auto-ping with refresh set to: {self.auto_ping}")
            self.log_message(
                f"[dim]Auto-ping with refresh {('enabled' if self.auto_ping else 'disabled')}[/]"
            )
    
    def _start_auto_refresh_timer(self) -> None:
        self._cancel_auto_refresh_timer()
        log.debug(f"Starting auto-refresh timer for {self.refresh_interval}s")
        self.refresh_timer = self.set_timer(
            self.refresh_interval, 
            self._auto_refresh, 
            name="refresh_timer"
        )
        self.countdown_seconds = self.refresh_interval
        self.countdown_timer = self.set_interval(
            1.0, 
            self._update_countdown, 
            name="countdown_timer"
        )
        self.last_refresh_time = time.time()
        
        log.debug(f"Auto-refresh timer scheduled for {self.refresh_interval}s from now")
        
    def _cancel_auto_refresh_timer(self) -> None:
        if self.refresh_timer:
            log.debug("Cancelling main refresh timer")
            if hasattr(self, 'remove_timer'):
                self.remove_timer(self.refresh_timer)
            else:
                self.refresh_timer.stop()
            self.refresh_timer = None
            
        if self.countdown_timer:
            log.debug("Cancelling countdown timer")
            if hasattr(self, 'remove_timer'):
                self.remove_timer(self.countdown_timer)
            else:
                self.countdown_timer.stop()
            self.countdown_timer = None
            
        self.last_refresh_time = None
        self.countdown_seconds = 0 
        
    def _update_countdown(self) -> None:
        if self.countdown_seconds > 0:
            self.countdown_seconds -= 1
            self.update_status(self.get_current_status_message())
            
            if self.countdown_seconds == 0:
                log.debug("Countdown reached zero, triggering data fetch")
                checkbox = self.query_one("#auto_refresh", expect_type=Checkbox)
                if checkbox.value:
                    if self.refresh_timer:
                        if hasattr(self, 'remove_timer'):
                            self.remove_timer(self.refresh_timer)
                        else:
                            self.refresh_timer.stop()
                        self.refresh_timer = None
                        
                    if self.countdown_timer:
                        if hasattr(self, 'remove_timer'):
                            self.remove_timer(self.countdown_timer)
                        else:
                            self.countdown_timer.stop()
                        self.countdown_timer = None
                        
                    self.fetch_tailscale_data()
                    
    def get_current_status_message(self) -> str:
        try:
            status = self.query_one("#tailscale-status")
            current_message = str(status.renderable)
            if " • Next refresh in " in current_message:
                return current_message.split(" • Next refresh in ")[0]
            return current_message
        except Exception as e:
            log.error(f"Error getting status message: {str(e)}")
            return "Ready"
            
    def update_status(self, message: str) -> None:
        try:
            status = self.query_one("#tailscale-status")
            base_message = message
            try:
                checkbox = self.query_one("#auto_refresh", expect_type=Checkbox)
                if checkbox.value and self.countdown_seconds > 0:
                    status.update(f"{base_message} • Next refresh in {self.countdown_seconds}s")
                else:
                    status.update(base_message)
            except Exception:
                status.update(base_message)
            log.info(f"Status update: {message}")
        except Exception as e:
            log.error(f"Error updating status: {str(e)}")
    
    def _auto_refresh(self) -> None:
        log.debug("Auto-refresh timer triggered")
        log.info("Auto-refreshing Tailscale data")
        self.fetch_tailscale_data()
        
    def _trigger_auto_ping(self) -> None:
        if not self.ping_in_progress:
            config = ConfigManager.load_config()
            auto_ping_delay = config.get("auto_ping_delay", 0)
            
            if auto_ping_delay > 0:
                log.debug(f"Scheduling auto-ping to run after {auto_ping_delay}s delay")
                self.log_message(f"[dim]Auto-ping will start in {auto_ping_delay} seconds[/]")
                
                if self.auto_ping_delay_timer:
                    if hasattr(self, 'remove_timer'):
                        self.remove_timer(self.auto_ping_delay_timer)
                    else:
                        self.auto_ping_delay_timer.stop()
                    self.auto_ping_delay_timer = None
                
                self.auto_ping_delay_timer = self.set_timer(auto_ping_delay, self._execute_delayed_ping)
            else:
                log.debug("Triggering immediate auto-ping after fetch")
                self.ping_tailscale_devices()
        else:
            log.warning("Skipping auto-ping because ping is already in progress")
            
    def _execute_delayed_ping(self) -> None:
        log.debug("Auto-ping delay timer expired, starting ping")
        self.log_message("[blue]Starting ping after configured delay[/]")
        self.ping_tailscale_devices()
        self.auto_ping_delay_timer = None
    
    def on_remove(self) -> None:
        log.debug("Tailscale integration screen being removed, cleaning up timers")
        self._cancel_auto_refresh_timer()
        
        if self.auto_ping_delay_timer:
            if hasattr(self, 'remove_timer'):
                self.remove_timer(self.auto_ping_delay_timer)
            else:
                self.auto_ping_delay_timer.stop()
            self.auto_ping_delay_timer = None
        
        super().on_remove()
        
    def update_status(self, message: str) -> None:
        try:
            status = self.query_one("#tailscale-status")
            base_message = message
            checkbox = self.query_one("#auto_refresh", expect_type=Checkbox)
            if checkbox.value and self.countdown_seconds > 0:
                status.update(f"{base_message} • Next refresh in {self.countdown_seconds}s")
            else:
                status.update(base_message)
            log.info(f"Status update: {message}")
        except Exception as e:
            log.error(f"Error updating status: {str(e)}")
    
    def on_remove(self) -> None:
        log.debug("Tailscale integration screen being removed, cleaning up timers")
        self._cancel_auto_refresh_timer()
        
        if self.auto_ping_delay_timer:
            if hasattr(self, 'remove_timer'):
                self.remove_timer(self.auto_ping_delay_timer)
            else:
                self.auto_ping_delay_timer.stop()
            self.auto_ping_delay_timer = None
        
        super().on_remove()
        
    @work(exclusive=True)
    async def _fetch_devices_worker(self, api_key: str, tailnet_org: str) -> None:
        try:
            start_time = time.time()
            
            log.info("Starting Tailscale devices fetch in worker")
            result = await TailscaleAPI.fetch_devices(api_key, tailnet_org)
            self.last_refresh_time = time.time()
            log.info(f"Fetch completed in {time.time() - start_time:.2f}s, processing results")
            if "error" in result:
                log.error(f"API Error: {result['error']}")
                self.update_status(f"[red]Error: {result['error']}[/]")
                return
                
            if "devices" in result and isinstance(result["devices"], list):
                devices = result["devices"]
                log.info(f"Got {len(devices)} devices from API")
                self._process_and_display_devices(devices)
                if self.auto_ping and not self.ping_in_progress:
                    log.info("Auto-ping is enabled, starting ping after data processing")
                    self.app.call_later(self._trigger_auto_ping)
            else:
                log.error("No devices found in API response")
                self.update_status("[red]No devices found in API response[/]")
        except Exception as e:
            log.error(f"Error in fetch worker: {str(e)}", exc_info=True)
            self.update_status(f"[red]Error: {str(e)}")
        finally:
            try:
                checkbox = self.query_one("#auto_refresh", expect_type=Checkbox)
                if checkbox and checkbox.value:
                    log.debug("Fetch completed, restarting refresh timer")
                    self._start_auto_refresh_timer()
            except Exception as e:
                log.error(f"Error restarting timers: {str(e)}")
            
    def on_worker_state_changed(self, event) -> None:
        if event.worker.state.name in ["SUCCESS", "ERROR", "CANCELLED"]:
            worker_name = str(event.worker.name)
            if "_fetch_and_process" in worker_name:
                log.info(f"Worker completed: {worker_name} with state {event.worker.state.name}")
                if event.worker.state.name == "ERROR":
                    log.error(f"Worker error: {event.worker.error}")
                    self.update_status(f"[red]Error: {event.worker.error}")
                    
    def _handle_result(self, result):
        log.info("Result handler triggered")
        log.debug(f"Result type: {type(result).__name__}")
        if isinstance(result, dict):
            log.info(f"Result keys: {list(result.keys())}")
        else:
            log.error(f"Result is not a dictionary: {type(result).__name__}")
            self.update_status("[red]Error: Invalid result format from API[/]")
            return
        
        if "error" in result:
            log.error(f"Error in fetch result: {result['error']}")
            self.update_status(f"[red]Error: {result['error']}[/]")
            return
        
        if "devices" in result and isinstance(result["devices"], list):
            devices = result["devices"]
            log.info(f"Got {len(devices)} devices directly from result")
            self._process_and_display_devices(devices)
        else:
            log.info("No devices in result, trying to load from file")
            yaml_file = TailscaleAPI.YAML_FILE
            try:
                with open(yaml_file, "r") as f:
                    try:
                        log.debug("Trying to parse file as JSON")
                        data = json.load(f)
                    except json.JSONDecodeError:
                        log.debug("Trying to parse file as YAML")
                        f.seek(0)
                        data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    log.error(f"Loaded data is not a dictionary: {type(data).__name__}")
                    self.update_status("[red]Invalid data format in device file[/]")
                    return
                log.debug(f"File data keys: {list(data.keys())}")
                devices = data.get("devices", [])
                log.info(f"Loaded {len(devices)} devices from file")
                self._process_and_display_devices(devices)
            except Exception as e:
                log.error(f"Error loading device data: {str(e)}")
                self.update_status("[red]Failed to load device data[/]")
                
    def _track_device_status_change(self, device, is_online):
        try:
            device_id = device.get('id')
            hostname = device.get('hostname', 'Unknown')
            
            if not device_id:
                return
            
            previous_state = self.previous_device_states.get(device_id)
            
            if previous_state is None:
                self.previous_device_states[device_id] = is_online
                return
            
            if previous_state != is_online:
                self.previous_device_states[device_id] = is_online
                
                timestamp = datetime.datetime.now().isoformat()
                tags = device.get('tags', [])
                group = "untagged" if not tags else tags[0]
                
                latency = 1.0 if is_online else 0.0
                LogManager.save_log_entry(
                    host=hostname,
                    group=f"tailscale:{group}",
                    latency=latency,
                    timestamp=timestamp,
                    success=is_online
                )
                
                # Also update the aggregated logs for the Tailnet FQDN if available
                tailnet_name = device.get('name')
                if tailnet_name and tailnet_name != hostname:
                    LogManager.save_log_entry(
                        host=f"{hostname} {tailnet_name}",
                        group=f"tailscale:{group}",
                        latency=latency,
                        timestamp=timestamp,
                        success=is_online
                    )
                
                status_text = "[green]came online[/]" if is_online else "[red]went offline[/]"
                
                ip_info = ""
                addresses = device.get('addresses', [])
                if addresses:
                    ipv4 = next((addr for addr in addresses if ":" not in addr), None)
                    ip_to_show = ipv4 if ipv4 else addresses[0]
                    ip_info = f" [dim]({ip_to_show})[/]"
                
                self.log_message(f"Device '[bold]{hostname}[/]'{ip_info} {status_text}")
                
                # Check against user-defined important tags instead of hardcoded values
                config = ConfigManager.load_config()
                important_tags = config.get("important_tailscale_tags", ["server", "important"])
                if not is_online and any(important_tag in tags for important_tag in important_tags):
                    self.log_message(f"[bold red]ALERT: Important device '{hostname}' is offline![/]")
                return True
        except Exception as e:
            log.error(f"Error tracking device status: {str(e)}", exc_info=True)
            return False
        return False

    def _process_and_display_devices(self, devices: List[Dict]) -> None:
        try:
            log.info(f"Processing {len(devices)} devices")
            state_changes = 0
            
            tree = self.query_one("#tailnet_tree")
            if not tree.root.children:
                self.log_message("Initializing device tree")
            else:
                self.log_message("Updating device tree")
            processed_nodes = set()
            processed_groups = set()
            processed_device_ids = set()
            tag_groups = {}
            devices_without_tags = []
            
            # Track total devices for aggregated metrics
            total_devices = len(devices)
            online_devices = 0
                
            for device in devices:
                device_id = device.get('id')
                if device_id:
                    processed_device_ids.add(device_id)
                last_seen = device.get('lastSeen', '')
                is_online = self._is_device_online(last_seen)
                
                if is_online:
                    online_devices += 1
                
                if device_id:
                    if self._track_device_status_change(device, is_online):
                        state_changes += 1
                
                has_tags = False
                tags = device.get('tags', [])
                
                if tags and isinstance(tags, list):
                    has_tags = True
                    for tag in tags:
                        tag_lower = tag.lower()
                        if tag_lower not in tag_groups:
                            tag_groups[tag_lower] = []
                        tag_groups[tag_lower].append(device)
                
                if not has_tags:
                    devices_without_tags.append(device)
            
            # Log overall network status
            if total_devices > 0:
                uptime_percentage = (online_devices / total_devices) * 100
                timestamp = datetime.datetime.now().isoformat()
                LogManager.save_log_entry(
                    host="TailscaleNetworkStatus",
                    group="tailscale:system",
                    latency=uptime_percentage,  # Using latency field to store uptime percentage
                    timestamp=timestamp,
                    success=(online_devices > 0)  # Consider network up if at least one device is online
                )
            # Rest of the existing processing code
            for device_id in list(self.previous_device_states.keys()):
                if device_id not in processed_device_ids:
                    if self.previous_device_states[device_id]:
                        device_name = "Unknown device"
                        for key, node in self.device_nodes.items():
                            if key.startswith(f"{device_id}:"):
                                if hasattr(node, "device_data"):
                                    device_data = getattr(node, "device_data")
                                    device_name = device_data.get("name", device_data.get("hostname", "Unknown device"))
                                break
                        timestamp = datetime.datetime.now().isoformat()
                        self.log_message(f"[bold red]Device '{device_name}' has disappeared from the Tailnet![/]")
                        LogManager.save_log_entry(
                            host=device_name,
                            group="tailscale:disappeared",
                            latency=0.0,
                            timestamp=timestamp,
                            success=False
                        )
                        state_changes += 1
                    del self.previous_device_states[device_id]
                
            for tag, tag_devices in tag_groups.items():
                processed_groups.add(tag.lower())
                if (tag.lower() in self.group_nodes):
                    group_node = self.group_nodes[tag.lower()]
                    group_node.label = f"{tag.capitalize()} ({len(tag_devices)})"
                else:
                    group_node = tree.root.add(f"{tag.capitalize()} ({len(tag_devices)})")
                    self.group_nodes[tag.lower()] = group_node
                for device in tag_devices:
                    device_id = device.get('id', str(id(device)))
                    node_key = f"{device_id}:{tag.lower()}"
                    processed_nodes.add(node_key)
                    self._update_or_add_device(group_node, device, tag.lower())
            
            if devices_without_tags:
                tag = "untagged"
                processed_groups.add(tag)
                if tag in self.group_nodes:
                    group_node = self.group_nodes[tag]
                    group_node.label = f"Untagged ({len(devices_without_tags)})"
                else:
                    group_node = tree.root.add(f"Untagged ({len(devices_without_tags)})")
                    self.group_nodes[tag] = group_node
                for device in devices_without_tags:
                    device_id = device.get('id', str(id(device)))
                    node_key = f"{device_id}:{tag}"
                    processed_nodes.add(node_key)
                    self._update_or_add_device(group_node, device, tag)
            
            groups_to_remove = set(self.group_nodes.keys()) - processed_groups
            for group in groups_to_remove:
                if group in self.group_nodes:
                    log.debug(f"Removing group node: {group}")
                    node = self.group_nodes[group]
                    node.remove()
                    del self.group_nodes[group]
            
            nodes_to_remove = set(self.device_nodes.keys()) - processed_nodes
            for node_key in nodes_to_remove:
                log.debug(f"Removing device node: {node_key}")
                node = self.device_nodes[node_key]
                node.remove()
                del self.device_nodes[node_key]
                    
                device_id = node_key.split(':')[0]
                if not any(key.startswith(f"{device_id}:") for key in self.device_nodes.keys()):
                    if device_id in self.device_states:
                        del self.device_states[device_id]
            
            online_count = sum(1 for state in self.device_states.values() if state)
            unique_device_count = len({key.split(':')[0] for key in self.device_nodes.keys()})
            offline_count = unique_device_count - online_count
            
            self._update_device_history(online_count)
            
            online_diff = online_count - self.previous_online_count
            offline_diff = offline_count - self.previous_offline_count
            
            online_change = ""
            if online_diff > 0 and not self.first_refresh:
                online_change = f" ([green]↑{online_diff}[/])"
            elif online_diff < 0 and not self.first_refresh:
                online_change = f" ([red]↓{abs(online_diff)}[/])"
                
            offline_change = ""
            if offline_diff > 0 and not self.first_refresh:
                offline_change = f" ([red]↑{offline_diff}[/])"
            elif offline_diff < 0 and not self.first_refresh:
                offline_change = f" ([green]↓{abs(offline_diff)}[/])"
                
            status_report = (
                f"Device Status: [green]{online_count} online{online_change}[/] • "
                f"[red]{offline_count} offline{offline_change}[/] • "
                f"[blue]{unique_device_count} total[/]"
            )
            self.log_message(status_report)
            
            if state_changes > 0:
                self.log_message(f"[yellow]{state_changes} device status changes detected[/]")
            
            self.previous_online_count = online_count
            self.previous_offline_count = offline_count
            self.first_refresh = False
            
            status_msg = f"Found {unique_device_count} unique devices • [green]{online_count} online{online_change}[/] • [red]{offline_count} offline{offline_change}[/]"
            if state_changes > 0:
                status_msg += f" • [yellow]{state_changes} status changes[/]"
            self.update_status(status_msg)
            self._update_device_history(online_count)
            self._update_sparkline()
        except Exception as e:
            log.error(f"Error processing device data: {str(e)}", exc_info=True)
            self.update_status(f"[red]Error: {str(e)}")
            
    def _update_or_add_device(self, parent_node, device, group):
        try:
            hostname = device.get('hostname', 'Unknown')
            device_id = device.get('id', str(id(device)))
            last_seen = device.get('lastSeen', '')
            is_online = self._is_device_online(last_seen)
            ping_info = ""
            if device_id in self.device_latencies:
                ping_is_alive, ping_latency = self.device_latencies[device_id]
                if ping_is_alive:
                    ping_info = f" [ping: {ping_latency:.1f}ms]"
                else:
                    ping_info = f" [ping: failed]"
            
            # Calculate uptime percentage
            uptime_percentage = LogManager.calculate_uptime(hostname, hours=24)
            uptime_info = f" [blue]{uptime_percentage:.1f}%[/]"
            
            if is_online:
                status_icon = "✓"
                label = f"{hostname} [green]{status_icon}[/]{ping_info}{uptime_info}"
            else:
                status_icon = "✗"
                if last_seen:
                    try:
                        last_seen_time = datetime.datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                        last_seen_formatted = last_seen_time.strftime("%d-%m-%Y %H:%M")
                        label = f"{hostname} [red]{status_icon}[/] [purple]({last_seen_formatted})[/]{ping_info}{uptime_info}"
                    except (ValueError, TypeError):
                        label = f"{hostname} [red]{status_icon}[/]{ping_info}{uptime_info}"
                else:
                    label = f"{hostname} [red]{status_icon}[/]{ping_info}{uptime_info}"
            
            self.device_states[device_id] = is_online
            
            node_key = f"{device_id}:{group}"
            
            if node_key in self.device_nodes:
                device_node = self.device_nodes[node_key]
                log.debug(f"Updating device node: {hostname} in group {group}")
                if device_node.parent != parent_node:
                    was_expanded = device_node.is_expanded
                    device_node.remove()
                    device_node = parent_node.add(label)
                    self.device_nodes[node_key] = device_node
                    setattr(device_node, "device_data", device)
                    if was_expanded:
                        self._add_device_details(device_node, device)
                        device_node.expand()
                else:
                    device_node.label = label
                    setattr(device_node, "device_data", device)
            else:
                log.debug(f"Adding new device node: {hostname} in group {group}")
                device_node = parent_node.add(label)
                self.device_nodes[node_key] = device_node
                setattr(device_node, "device_data", device)
        except Exception as e:
            log.error(f"Error updating device node: {str(e)}", exc_info=True)
            
    def _is_device_online(self, last_seen_str):
        if not last_seen_str:
            return False
        
        try:
            log.debug(f"Checking online status for timestamp: {last_seen_str}")
            last_seen_time = datetime.datetime.fromisoformat(last_seen_str.replace('Z', '+00:00'))
            now = datetime.datetime.now(datetime.timezone.utc)
            time_diff = now - last_seen_time
            log.debug(f"Time difference: {time_diff.total_seconds()} seconds")
                
            is_online = time_diff < datetime.timedelta(minutes=5)
            log.debug(f"Device online status: {is_online}")
            return is_online
        except (ValueError, TypeError) as e:
            log.warning(f"Error parsing lastSeen time '{last_seen_str}': {str(e)}")
            return False
            
    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        try:
            node = event.node
            log.debug(f"Node selected: {node.label if hasattr(node, 'label') else 'unknown'}")
            if node.parent == self.query_one("#tailnet_tree").root:
                log.debug("This is a tag group node, not modifying expand state")
                self.selected_device = None
                self.selected_device_name = None
                self._update_sparkline()
                return
                
            device = getattr(node, "device_data", None)
            if device:
                self.selected_device = device.get('id')
                self.selected_device_name = device.get('hostname', 'Unknown')
                self._update_sparkline()
            else:
                log.warning(f"No device data found for node: {node.label}")
                self.selected_device = None
                self.selected_device_name = None
                self._update_sparkline()
        except Exception as e:
            log.error(f"Error in on_tree_node_selected: {str(e)}", exc_info=True)
            
    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        try:
            node = event.node
            log.debug(f"Node expanded: {node.label if hasattr(node, 'label') else 'unknown'}")
            # If this is a device node and it doesn't have any children yet, add details
            if node.parent != self.query_one("#tailnet_tree").root:  # Not a tag group node
                device = getattr(node, "device_data", None)
                if device and not node.children:
                    self._add_device_details(node, device)
        except Exception as e:
            log.error(f"Error in on_tree_node_expanded: {str(e)}", exc_info=True)
            
    def on_key(self, event) -> None:
        # Handle space key to toggle node expansion
        if event.key == "space":
            tree = self.query_one("#tailnet_tree", Tree)
            if tree.cursor_node:
                tree.cursor_node.toggle()
                event.prevent_default()
                event.stop()
            
    def _add_device_details(self, node, device):
        try:
            addresses = device.get('addresses', [])
            ipv4_addresses = []
            ipv6_addresses = []
            
            for address in addresses:
                if ":" not in address:
                    ipv4_addresses.append(address)
                else:
                    ipv6_addresses.append(address)
                    
            if ipv4_addresses:
                node.add_leaf(f"[bold]IPv4: {ipv4_addresses[0]}[/]")
            node = node.add("Device Information")
            node.add_leaf(f"Name: {device.get('name', 'Unknown')}")
            node.add_leaf(f"Hostname: {device.get('hostname', 'Unknown')}")
            node.expand()
            
            ip_node = node.add("IP Addresses")
            for ipv4 in ipv4_addresses:
                ip_node.add_leaf(f"IPv4: {ipv4}")
            for ipv6 in ipv6_addresses:
                ip_node.add_leaf(f"IPv6: {ipv6}")
            ip_node.expand()
            
            status_node = node.add("Status")
            for date_field in ['created', 'expires', 'lastSeen']:
                if date_field in device:
                    try:
                        date_str = device[date_field]
                        dt = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S")
                        status_node.add_leaf(f"{date_field.capitalize()}: {formatted_date}")
                    except (ValueError, TypeError) as e:
                        log.warning(f"Error formatting date '{date_field}': {str(e)}")
                        status_node.add_leaf(f"{date_field.capitalize()}: {device.get(date_field, 'Unknown')}")
            
            # Add uptime information
            hostname = device.get('hostname', 'Unknown')
            uptime_percentage = LogManager.calculate_uptime(hostname, hours=24)
            status_node.add_leaf(f"24h Uptime: [blue]{uptime_percentage:.1f}%[/]")
            
            update_available = device.get('updateAvailable', False)
            update_text = "[yellow]Available[/]" if update_available else "[green]Up to date[/]"
            status_node.add_leaf(f"Update: {update_text}")
                    
            if 'clientVersion' in device:
                status_node.add_leaf(f"Client Version: {device['clientVersion']}")
                
            status_node.expand()
            
            if 'tags' in device and device['tags']:
                tags_node = node.add("Tags")
                for tag in device['tags']:
                    tags_node.add_leaf(tag)
                tags_node.expand()
            
            online_status = self._is_device_online(device.get('lastSeen', ''))
            status_icon = "[green]✓ Online[/]" if online_status else "[red]✗ Offline[/]"
            node.add_leaf(f"Status: {status_icon}")
            node.expand()
        except Exception as e:
            log.error(f"Error adding device details: {str(e)}", exc_info=True)
            node.add_leaf(f"[red]Error loading details: {str(e)}[/]")
            
    def fetch_tailscale_data(self) -> None:
        config = ConfigManager.load_config()
        api_key = config.get("tailscale_api_key")
        tailnet_org = config.get("tailnet_org")
        
        log.info("Starting Tailscale data fetch")
        log.debug(f"Using tailnet org: {tailnet_org}")
        
        if not api_key:
            log.warning("No Tailscale API key configured")
            self.update_status("[red]No Tailscale API key set. Please add one in Settings.[/]")
            return
        
        if not tailnet_org:
            log.warning("No Tailnet organization configured")
            self.update_status("[red]No Tailnet organization set. Please configure it in Settings.[/]")
            return
        
        self.update_status("[yellow]Refreshing device data...[/]")
        
        if self.auto_ping:
            config = ConfigManager.load_config()
            auto_ping_delay = config.get("auto_ping_delay", 0)
            if auto_ping_delay > 0:
                log.debug(f"Auto-ping will run after fetch with {auto_ping_delay}s delay")
                log_widget = self.query_one("#tailscale-log", RichLog)
                log_widget.write(
                    f"[dim]Auto-ping will run {auto_ping_delay}s after device data refresh[/]",
                    expand=True,
                    scroll_end=True
                )
            else:
                log.debug("Auto-ping will run immediately after fetch")
                log_widget = self.query_one("#tailscale-log", RichLog)
                log_widget.write(
                    "[dim]Auto-ping will run immediately after device data refresh[/]",
                    expand=True,
                    scroll_end=True
                )
                
        self._fetch_devices_worker(api_key, tailnet_org)
        
    def _track_device_status_change(self, device, is_online):
        try:
            device_id = device.get('id')
            hostname = device.get('hostname', 'Unknown')
            
            if not device_id:
                return
            
            previous_state = self.previous_device_states.get(device_id)
            
            if previous_state is None:
                self.previous_device_states[device_id] = is_online
                return
            
            if previous_state != is_online:
                self.previous_device_states[device_id] = is_online
                
                timestamp = datetime.datetime.now().isoformat()
                tags = device.get('tags', [])
                group = "untagged" if not tags else tags[0]
                
                latency = 1.0 if is_online else 0.0
                LogManager.save_log_entry(
                    host=hostname,
                    group=f"tailscale:{group}",
                    latency=latency,
                    timestamp=timestamp,
                    success=is_online
                )
                
                # Also update the aggregated logs for the Tailnet FQDN if available
                tailnet_name = device.get('name')
                if tailnet_name and tailnet_name != hostname:
                    LogManager.save_log_entry(
                        host=f"{hostname} {tailnet_name}",
                        group=f"tailscale:{group}",
                        latency=latency,
                        timestamp=timestamp,
                        success=is_online
                    )
                
                status_text = "[green]came online[/]" if is_online else "[red]went offline[/]"
                
                ip_info = ""
                addresses = device.get('addresses', [])
                if addresses:
                    ipv4 = next((addr for addr in addresses if ":" not in addr), None)
                    ip_to_show = ipv4 if ipv4 else addresses[0]
                    ip_info = f" [dim]({ip_to_show})[/]"
                
                self.log_message(f"Device '[bold]{hostname}[/]'{ip_info} {status_text}")
                
                # Check against user-defined important tags instead of hardcoded values
                config = ConfigManager.load_config()
                important_tags = config.get("important_tailscale_tags", ["server", "important"])
                if not is_online and any(important_tag in tags for important_tag in important_tags):
                    self.log_message(f"[bold red]ALERT: Important device '{hostname}' is offline![/]")
                return True
        except Exception as e:
            log.error(f"Error tracking device status: {str(e)}", exc_info=True)
            return False
        return False

    def _update_device_history(self, online_count: int) -> None:
        """
        Update aggregate device history logs
        """
        try:
            timestamp = datetime.datetime.now().isoformat()
            # Log overall Tailscale network health
            total_devices = self.previous_online_count + self.previous_offline_count
            if total_devices > 0:
                uptime_percentage = (self.previous_online_count / total_devices) * 100
                # Log a synthetic entry to track overall Tailscale health
                LogManager.save_log_entry(
                    host="TailscaleNetwork",
                    group="tailscale:aggregate",
                    latency=uptime_percentage,  # Using latency field to store uptime percentage
                    timestamp=timestamp,
                    success=True
                )
                
                # Getting average latency from all online devices
                total_latency = 0
                device_count = 0
                for device_id, (is_alive, latency) in self.device_latencies.items():
                    if is_alive and latency > 0:
                        total_latency += latency
                        device_count += 1
                avg_latency = total_latency / device_count if device_count > 0 else 0
                
                # Log average network latency
                if avg_latency > 0:
                    LogManager.save_log_entry(
                        host="TailscaleLatency",
                        group="tailscale:metrics",
                        latency=avg_latency,
                        timestamp=timestamp,
                        success=True
                    )
        except Exception as e:
            log.error(f"Error updating device history: {str(e)}", exc_info=True)
            
    @work(exclusive=True)
    async def ping_tailscale_devices(self) -> None:
        if self.ping_in_progress:
            log.warning("Ping already in progress, ignoring request")
            return
        
        try:
            self.ping_in_progress = True
            self.update_status("[yellow]Pinging Tailscale devices...[/]")
            log_widget = self.query_one("#tailscale-log", RichLog)
            log_widget.write(
                "[blue]━━━━━━━━━━━━━━━ Starting Device Ping ━━━━━━━━━━━━━━━[/]", 
                expand=True,
                scroll_end=True,
                animate=True
            )
            
            config = ConfigManager.load_config()
            ping_count = config["ping_count"]
            
            if not self.device_nodes:
                log_widget.write(
                    "[yellow]No devices loaded yet. Fetch device data first.[/]",
                    expand=True,
                    scroll_end=True
                )
                self.update_status("[yellow]No devices to ping. Fetch device data first.[/]")
                self.ping_in_progress = False
                return
            
            device_ips = {}
            for node_key, node in self.device_nodes.items():
                device_data = getattr(node, "device_data", None)
                if device_data:
                    device_id = device_data.get('id')
                    if device_id and device_id not in device_ips:
                        addresses = device_data.get('addresses', [])
                        for addr in addresses:
                            if ":" not in addr:
                                device_ips[device_id] = addr
                                break
                        if device_id not in device_ips and addresses:
                            device_ips[device_id] = addresses[0]
            
            tasks = []
            total_devices = len(device_ips)
            log_widget.write(
                f"[blue]Pinging {total_devices} unique devices with {ping_count} packets each:[/]",
                expand=True,
                scroll_end=True
            )
            for device_id, ip in device_ips.items():
                tasks.append(asyncio.create_task(self._ping_device(device_id, ip, ping_count)))
                
            if tasks:
                await asyncio.gather(*tasks)
                self._update_tree_with_latencies()
                
            success_count = sum(1 for is_alive, _ in self.device_latencies.values() if is_alive)
            failed_count = len(self.device_latencies) - success_count
                
            summary = (
                f"[green]✓ {success_count} successful[/]"
                f" • [red]✗ {failed_count} failed[/]"
                f" • [blue]{len(self.device_latencies)} total[/]"
            )
            
            log_widget.write(
                f"[blue]━━━━━━━━━━━━━━━ Ping Complete ━━━━━━━━━━━━━━━[/]",
                expand=True,
                scroll_end=True
            )
            
            log_widget.write(
                summary,
                expand=True,
                scroll_end=True
            )
            
            self.update_status(f"[green]Ping complete: {summary}[/]")
            # Update the sparkline if we have a selected device
            if self.selected_device_name:
                self._update_sparkline()
            
        except Exception as e:
            log.error(f"Error pinging Tailscale devices: {str(e)}")
            
            log_widget = self.query_one("#tailscale-log", RichLog)
            log_widget.write(
                f"[bold red]Error during ping: {str(e)}[/]",
                expand=True,
                scroll_end=True,
                animate=True
            )
            self.update_status(f"[red]Error during ping: {str(e)}[/]")
        finally:
            self.ping_in_progress = False
            
    async def _ping_device(self, device_id, ip, ping_count) -> None:
        device_name = "Unknown"
        tailnet_name = None
        for node_key, node in self.device_nodes.items():
            if node_key.startswith(f"{device_id}:"):
                device_data = getattr(node, "device_data", None)
                if device_data:
                    device_name = device_data.get('hostname', 'Unknown')
                    tailnet_name = device_data.get('name', None)
                break
        try:
            async with self.ping_semaphore:
                is_alive, latency = await PingMonitor.ping(ip, ping_count)
                timestamp = datetime.datetime.now().isoformat()
                self.device_latencies[device_id] = (is_alive, latency)
                
                group = "tailscale"
                for node_key in self.device_nodes:
                    if node_key.startswith(f"{device_id}:"):
                        parts = node_key.split(":")
                        if len(parts) > 1:
                            group = parts[1]
                        break
                
                # Log for the hostname
                LogManager.save_log_entry(
                    host=device_name,
                    group=f"tailscale:{group}",
                    latency=latency if is_alive else 0.0,
                    timestamp=timestamp,
                    success=is_alive
                )
                
                # Also log with full tailnet name for aggregated logs if available
                if tailnet_name and tailnet_name != device_name:
                    LogManager.save_log_entry(
                        host=f"{device_name} {tailnet_name}",
                        group=f"tailscale:{group}",
                        latency=latency if is_alive else 0.0,
                        timestamp=timestamp,
                        success=is_alive
                    )
                
                if is_alive:
                    status_icon = "[green]✓[/]"
                    latency_text = f"[green]{latency:.1f}ms[/]"
                else:
                    status_icon = "[red]✗[/]"
                    latency_text = "[red]failed[/]"
                
                name_display = device_name[:20].ljust(20)
                ip_display = ip.ljust(15)
                
                log_widget = self.query_one("#tailscale-log", RichLog)
                log_widget.write(
                    f"{status_icon} {name_display} {ip_display} {latency_text}",
                    expand=True,
                    scroll_end=True
                )
                
        except Exception as e:
            log.error(f"Error pinging device {device_name} ({ip}): {str(e)}")
            log_widget = self.query_one("#tailscale-log", RichLog)
            log_widget.write(
                f"[red]✗ Failed to ping {device_name} ({ip}): {str(e)}[/]",
                expand=True,
                scroll_end=True
            )
            self.device_latencies[device_id] = (False, 0.0)
            
            # Log the failure
            timestamp = datetime.datetime.now().isoformat()
            LogManager.save_log_entry(
                host=device_name,
                group="tailscale:error",
                latency=0.0,
                timestamp=timestamp,
                success=False
            )
            
            # Also log with full tailnet name for aggregated logs if available
            if tailnet_name and tailnet_name != device_name:
                LogManager.save_log_entry(
                    host=f"{device_name} {tailnet_name}",
                    group="tailscale:error",
                    latency=0.0,
                    timestamp=timestamp,
                    success=False
                )
                
    def _update_tree_with_latencies(self) -> None:
        try:
            if not self.device_latencies:
                return
            
            for node_key, node in self.device_nodes.items():
                device_id = node_key.split(":")[0]
                if device_id not in self.device_latencies:
                    continue
                
                is_alive, latency = self.device_latencies[device_id]
                
                current_label = node.label
                
                # Preserve the uptime percentage when updating the label
                uptime_info = ""
                if " [blue]" in current_label and "%[/]" in current_label:
                    uptime_start = current_label.find(" [blue]")
                    uptime_end = current_label.find("%[/]", uptime_start) + 4
                    uptime_info = current_label[uptime_start:uptime_end]
                
                if " [ping:" in current_label:
                    current_label = current_label.split(" [ping:")[0]
                
                # Preserve the uptime if it exists but was stripped off with the ping info
                if not uptime_info and " [blue]" in current_label and "%[/]" in current_label:
                    uptime_start = current_label.find(" [blue]")
                    uptime_end = current_label.find("%[/]", uptime_start) + 4
                    uptime_info = current_label[uptime_start:uptime_end]
                
                has_purple_timestamp = "[purple]" in current_label
                
                if is_alive:
                    ping_info = f" [ping: {latency:.1f}ms]"
                else:
                    ping_info = ""
                    if has_purple_timestamp:
                        parts = current_label.split("[/]")
                        if len(parts) >= 2:
                            timestamp_end_idx = current_label.find("[/]", current_label.find("[purple]")) + 3
                            base_label = current_label[:timestamp_end_idx]
                            node.label = base_label + ping_info + uptime_info
                            continue
                    else:
                        ping_info = ""
                if not ping_info:
                    continue
                
                if "[green]✓[/]" in current_label:
                    base_label = current_label.split("[green]✓[/]")[0] + "[green]✓[/]"
                    node.label = base_label + ping_info + uptime_info
                elif "[red]✗[/]" in current_label:
                    if has_purple_timestamp:
                        parts = current_label.split("[/]")
                        if len(parts) >= 2:
                            timestamp_end_idx = current_label.find("[/]", current_label.find("[purple]")) + 3
                            base_label = current_label[:timestamp_end_idx]
                            node.label = base_label + ping_info + uptime_info
                            continue
                    else:
                        base_label = current_label.split("[red]✗[/]")[0] + "[red]✗[/]"
                        node.label = base_label + ping_info + uptime_info
                else:
                    node.label = current_label + ping_info + uptime_info
        except Exception as e:
            log.error(f"Error updating tree with latencies: {str(e)}")
            
    def _update_sparkline(self) -> None:
        """
        Update the sparkline with latency data for the selected device
        """
        try:
            sparkline = self.query_one("#device-sparkline", Sparkline)
            sparkline_title = self.query_one("#sparkline-title", Static)
            
            if not self.selected_device or not self.selected_device_name:
                sparkline_title.update("Select a device to view latency history")
                sparkline.data = []
                return
            
            # Get data from aggregated logs
            latency_data = LogManager.get_latency_history(self.selected_device_name)
            if not latency_data or all(x == 0 for x in latency_data):
                sparkline_title.update(f"No latency data for {self.selected_device_name}")
                sparkline.data = [0] * 10  # Empty sparkline
                return
            
            # Update the sparkline with the data
            sparkline.data = latency_data
            
            # Calculate some statistics for the title
            avg_latency = sum(latency_data) / len(latency_data) if latency_data else 0
            max_latency = max(latency_data) if latency_data else 0
            min_latency = min(x for x in latency_data if x > 0) if any(x > 0 for x in latency_data) else 0
            
            sparkline_title.update(
                f"Latency for {self.selected_device_name}: "
                f"avg={avg_latency:.1f}ms, min={min_latency:.1f}ms, max={max_latency:.1f}ms ({len(latency_data)} points)"
            )
        except Exception as e:
            log.error(f"Error updating sparkline: {str(e)}", exc_info=True)
            sparkline_title = self.query_one("#sparkline-title", Static)
            sparkline_title.update(f"Error updating latency chart: {str(e)}")

class TailscaleTagSettingsScreen(Screen):
    """Screen for managing important Tailscale tags that trigger alerts."""
    def __init__(self):
        super().__init__()
        self.all_tags = set()
        self.available_tags = []
        self.important_tags = []
        self.load_tags()
        
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static("Important Tailscale Tags", id="tags-title"),
            Static("Select tags that should trigger alerts when devices go offline", id="tags-description"),
            Container(
                SelectionList[str](
                    *self.available_tags,
                    id="tag-selection"
                ),
                id="selection-container"
            ),
            Container(
                Button("Add New Tag", id="add_tag", variant="primary", classes="btn-violet"),
                Button("Save", id="save", variant="success", classes="btn-lime"),
                Button("Cancel", id="cancel", variant="error", classes="btn-coral"),
                id="buttons",
            )
        )
        yield Footer()
        
    def load_tags(self):
        """Load saved important tags and all discovered tags."""
        config = ConfigManager.load_config()
        self.important_tags = config.get("important_tailscale_tags", ["server", "important"])
        
        # Try to load all tags from cached Tailscale data
        all_tags = set(self.important_tags)  # Start with current important tags
        
        # Add tags from last fetched Tailscale data if available
        if os.path.exists(TailscaleAPI.YAML_FILE):
            try:
                with open(TailscaleAPI.YAML_FILE, 'r') as yaml_file:
                    data = yaml.safe_load(yaml_file)
                    if "devices" in data:
                        for device in data["devices"]:
                            if "tags" in device and isinstance(device["tags"], list):
                                all_tags.update(device["tags"])
            except Exception as e:
                log.error(f"Error loading tags from Tailscale data: {str(e)}")
                
        self.all_tags = all_tags
        self.available_tags = [(tag, tag, tag in self.important_tags) for tag in sorted(all_tags)]
        
    def on_mount(self) -> None:
        selection_list = self.query_one("#tag-selection", SelectionList)
        selection_list.border_title = "Tags (Space to toggle)"
        
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.app.pop_screen()
        elif event.button.id == "save":
            self.save_settings()
        elif event.button.id == "add_tag":
            self.add_new_tag()
    
    def add_new_tag(self) -> None:
        """Open a prompt to add a new tag."""
        self.app.push_screen(
            TextPrompt(
                "Enter new tag name:",
                "",
                "new_tag"
            ),
            callback=self.handle_new_tag
        )
        
    def handle_new_tag(self, result: str) -> None:
        """Handle adding a new tag from the prompt."""
        if result and result.strip():
            new_tag = result.strip().lower()
            if new_tag not in self.all_tags:
                selection_list = self.query_one("#tag-selection", SelectionList)
                selection_list.add_option((new_tag, new_tag, True))
                self.all_tags.add(new_tag)
                self.notify(f"Added new tag: {new_tag}")
            else:
                self.notify(f"Tag '{new_tag}' already exists")
        
    def save_settings(self) -> None:
        """Save selected important tags to config."""
        try:
            selection_list = self.query_one("#tag-selection", SelectionList)
            selected_tags = selection_list.selected
            
            if not selected_tags:
                self.notify("Warning: No important tags selected, using defaults", severity="warning")
                selected_tags = ["server", "important"]
            
            # Get current config and update just the important tags
            config = ConfigManager.load_config()
            
            # Save the tags to the config
            ConfigManager.save_config(
                config["inventory_path"],
                config["check_interval"],
                config["max_latency"],
                config["ping_count"],
                config["max_threads"],
                config["tailscale_api_key"],
                config["key_expiry_days"],
                config["tailnet_org"],
                config["auto_ping_delay"],
                selected_tags
            )
            
            self.notify("Important tags saved")
            self.app.pop_screen()
        except Exception as e:
            log.error(f"Error saving important tags: {str(e)}")
            self.notify(f"Error saving tags: {str(e)}", severity="error")

if __name__ == "__main__":
    app = PingPanel()
    app.run()

### Copyright CrabMan Stan

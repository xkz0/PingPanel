from textual.app import App, ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Header, Footer, Button, Static, Tree, Input
import yaml
import platform
import subprocess
from typing import Dict, List, Tuple
import os
import json
from textual.worker import Worker
import asyncio
import time
import datetime

class ConfigManager:
    CONFIG_FILE = "pingpanel_config.json"

    @staticmethod
    def save_config(
        inventory_path: str, 
        check_interval: int = 60,
        max_latency: int = 1000,
        ping_count: int = 3,
        max_threads: int = 10  # Added parameter
    ) -> None:
        with open(ConfigManager.CONFIG_FILE, "w") as f:
            json.dump({
                "inventory_path": inventory_path,
                "check_interval": check_interval,
                "max_latency": max_latency,
                "ping_count": ping_count,
                "max_threads": max_threads  # Added to config
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
                    "max_threads": config.get("max_threads", 10)  # Added default
                }
        return {
            "inventory_path": "", 
            "check_interval": 60,
            "max_latency": 1000,
            "ping_count": 3,
            "max_threads": 10  # Added default
        }

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
        
        # Update current logs
        existing_logs = []
        if os.path.exists(LogManager.CURRENT_LOG_FILE):
            try:
                with open(LogManager.CURRENT_LOG_FILE, 'r') as f:
                    existing_logs = json.load(f)
            except json.JSONDecodeError:
                existing_logs = []
        
        # Remove entries older than 1 hour
        current_time = datetime.datetime.fromisoformat(timestamp)
        hour_ago = current_time - datetime.timedelta(hours=1)
        existing_logs = [
            log for log in existing_logs 
            if datetime.datetime.fromisoformat(log["timestamp"]) > hour_ago
        ]
        existing_logs.append(log_entry)
        
        # Save current logs
        with open(LogManager.CURRENT_LOG_FILE, 'w') as f:
            json.dump(existing_logs, f, indent=2)
        
        # Immediately update aggregated logs
        LogManager.update_aggregated_logs(host, group, latency, timestamp, success)

    @staticmethod
    def update_aggregated_logs(host: str, group: str, latency: float, timestamp: str, success: bool) -> None:
        aggregated_logs = {}
        if os.path.exists(LogManager.AGGREGATED_LOG_FILE):
            try:
                with open(LogManager.AGGREGATED_LOG_FILE, 'r') as f:
                    # Convert list to dict with host as key for easier updating
                    aggregated_logs = {entry["host"]: entry for entry in json.load(f)}
            except json.JSONDecodeError:
                aggregated_logs = {}
        
        # Update or create entry for this host
        if host in aggregated_logs:
            entry = aggregated_logs[host]
            total_checks = entry["total_checks"] + 1
            if success:
                # Update running average for latency
                old_total = entry["avg_latency"] * entry["total_checks"]
                new_avg = (old_total + latency) / total_checks
                entry["avg_latency"] = new_avg
                entry["last_successful"] = timestamp
            
            # Update uptime percentage
            successful_checks = (entry["uptime_percentage"] / 100 * entry["total_checks"])
            if success:
                successful_checks += 1
            entry["uptime_percentage"] = (successful_checks / total_checks) * 100
            entry["total_checks"] = total_checks
            entry["last_check"] = timestamp
            
        else:
            # Create new entry
            aggregated_logs[host] = {
                "host": host,
                "group": group,
                "avg_latency": latency if success else 0,
                "uptime_percentage": 100 if success else 0,
                "total_checks": 1,
                "last_check": timestamp,
                "last_successful": timestamp if success else None
            }
        
        # Save updated aggregated logs
        with open(LogManager.AGGREGATED_LOG_FILE, 'w') as f:
            json.dump(list(aggregated_logs.values()), f, indent=2)

    @staticmethod
    def calculate_uptime(host: str, hours: int = 24) -> float:
        current_time = datetime.datetime.now()
        threshold_time = current_time - datetime.timedelta(hours=hours)
        
        # Get current logs
        current_logs = []
        if os.path.exists(LogManager.CURRENT_LOG_FILE):
            try:
                with open(LogManager.CURRENT_LOG_FILE, 'r') as f:
                    current_logs = json.load(f)
            except json.JSONDecodeError:
                current_logs = []

        # Get aggregated logs
        aggregated_logs = []
        if os.path.exists(LogManager.AGGREGATED_LOG_FILE):
            try:
                with open(LogManager.AGGREGATED_LOG_FILE, 'r') as f:
                    aggregated_logs = json.load(f)
            except json.JSONDecodeError:
                aggregated_logs = []

        # Calculate uptime from current logs
        recent_logs = [
            log for log in current_logs
            if log['host'] == host and
            datetime.datetime.fromisoformat(log['timestamp']) > threshold_time
        ]
        
        # Get the relevant aggregated log entry
        host_aggregate = next(
            (log for log in aggregated_logs if log['host'] == host and
             datetime.datetime.fromisoformat(log['last_check']) > threshold_time),
            None
        )

        if not recent_logs and not host_aggregate:
            return 0.0

        # Calculate total uptime percentage
        total_checks = len(recent_logs)
        total_successes = sum(1 for log in recent_logs if log['success'])

        if host_aggregate:
            total_checks += host_aggregate['total_checks']
            total_successes += (host_aggregate['total_checks'] * host_aggregate['uptime_percentage'] / 100)

        return (total_successes / total_checks) * 100 if total_checks > 0 else 0.0

    @staticmethod
    def get_last_online(host: str) -> str:
        # Check aggregated logs first for last successful timestamp
        if os.path.exists(LogManager.AGGREGATED_LOG_FILE):
            try:
                with open(LogManager.AGGREGATED_LOG_FILE, 'r') as f:
                    logs = json.load(f)
                    for log in logs:
                        if log['host'] == host and log.get('last_successful'):
                            return datetime.datetime.fromisoformat(log['last_successful']).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        
        # If no aggregated log found, check current logs
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
            
            # Parse average latency from ping output
            output = stdout.decode()
            if platform.system().lower() == "windows":
                if "Average" in output:
                    avg_line = [line for line in output.split("\n") if "Average" in line][0]
                    latency = float(avg_line.split("=")[-1].strip("ms"))
                else:
                    latency = 0.0
            else:  # Linux/Unix
                if "rtt" in output:
                    avg_line = output.split("\n")[-2]
                    latency = float(avg_line.split("/")[4])
                else:
                    latency = 0.0
            
            return process.returncode == 0, latency
        except (asyncio.TimeoutError, Exception):
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Status: Ready", id="status"),
            Container(
                Static("[green]Up: 0[/]", id="total_up"),
                Static("[red]Down: 0[/]", id="total_down"),
                id="totals"
            ),
            Tree("Hosts", id="host_tree"),
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
        
        # Initialize counters to 0
        self.total_up = 0
        self.total_down = 0
        
        for group, hosts in self.groups.items():
            group_node = tree.root.add(group)
            for hostname, ip in hosts:
                label = f"{hostname} ({ip}) ⟳"
                host_node = group_node.add(label)
                self.host_nodes[(group, hostname)] = host_node
                # Set initial state as None
                self.host_states[(group, hostname)] = None

    async def check_single_host(self, group: str, hostname: str, ip: str) -> None:
        async with self.ping_semaphore:
            host_node = self.host_nodes[(group, hostname)]
            status = self.query_one("#status")
            
            config = ConfigManager.load_config()
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
            
            # Update totals only on state changes or first check
            if prev_state is None:
                # First check for this host
                if is_acceptable:
                    self.total_up += 1
                else:
                    self.total_down += 1
            elif prev_state != is_acceptable:
                # State changed - ensure we don't go negative
                if is_acceptable and self.total_down > 0:
                    self.total_up += 1
                    self.total_down -= 1
                elif not is_acceptable and self.total_up > 0:
                    self.total_down += 1
                    self.total_up -= 1
                change_text = f"{hostname} is now {'ONLINE' if is_acceptable else 'OFFLINE'}"
                self.last_state_change = change_text
                status.update(f"Status: {change_text}")
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
            
            # Update the totals display after each host check
            self.query_one("#total_up").update(f"[green]Up: {self.total_up}[/]")
            self.query_one("#total_down").update(f"[red]Down: {self.total_down}[/]")

    async def check_hosts(self) -> None:
        status = self.query_one("#status")
        status.update("Status: Starting host checks...")
        
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
            
        except Exception as e:
            error_msg = f"Error during check: {str(e)}"
            print(error_msg)
            status.update(f"Status: {error_msg}")

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

class SettingsScreen(Screen):
    def compose(self) -> ComposeResult:
        config = ConfigManager.load_config()
        yield Header()
        yield Container(
            Static("Settings"),
            Static("Inventory File Path:", id="inventory_label"),
            Button(config["inventory_path"] or "Not Set", id="inventory_path"),
            Static("Check Interval (seconds):", id="interval_label"),
            Button(str(config["check_interval"]), id="interval"),
            Static("Max Acceptable Latency (ms):", id="latency_label"),
            Button(str(config["max_latency"]), id="max_latency"),
            Static("Ping Packet Count:", id="count_label"),
            Button(str(config["ping_count"]), id="ping_count"),
            Static("Max Concurrent Pings:", id="threads_label"),
            Button(str(config["max_threads"]), id="max_threads"),  # Added control
            Button("Save", id="save"),
            Button("Back", id="back"),
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id in ["interval", "max_latency", "ping_count", "inventory_path", "max_threads"]:  # Added max_threads
            prompts = {
                "interval": "Enter check interval in seconds:",
                "max_latency": "Enter maximum acceptable latency in milliseconds:",
                "ping_count": "Enter number of ping packets to send:",
                "inventory_path": "Enter path to Ansible inventory file:",
                "max_threads": "Enter maximum number of concurrent pings:"  # Added prompt
            }
            self.app.push_screen(
                TextPrompt(
                    prompts[event.button.id],
                    str(self.query_one(f"#{event.button.id}").label),
                    event.button.id
                )
            )
        elif event.button.id == "save":
            try:
                interval = int(str(self.query_one("#interval").label))
                max_latency = int(str(self.query_one("#max_latency").label))
                ping_count = int(str(self.query_one("#ping_count").label))
                max_threads = int(str(self.query_one("#max_threads").label))  # Added parsing
                inventory_path = str(self.query_one("#inventory_path").label)
                
                if interval < 1 or max_latency < 1 or ping_count < 1 or max_threads < 1:  # Added validation
                    raise ValueError("All values must be positive")
                
                if not os.path.exists(inventory_path):
                    raise ValueError("Inventory file does not exist")
                
                ConfigManager.save_config(
                    inventory_path,
                    interval,
                    max_latency,
                    ping_count,
                    max_threads  # Added to save
                )
                self.app.pop_screen()
            except ValueError as e:
                self.notify(str(e))

class TextPrompt(Screen):
    def __init__(self, question: str, default_value: str, setting_id: str):
        super().__init__()
        self.question = question
        self.default_value = default_value
        self.setting_id = setting_id  # Store which setting we're updating

    def compose(self) -> ComposeResult:
        yield Static(self.question)
        yield Input(value=self.default_value, id="input")
        yield Button("OK", id="ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            value = self.query_one("#input").value
            settings_screen = self.app.screen_stack[-2]
            # Update the correct button based on setting_id
            settings_screen.query_one(f"#{self.setting_id}").label = value
            self.app.pop_screen()

class MainMenu(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Container(
                Static("PingPanel", id="title-message"),
                Static("Courtesy of CrabManStan", id="courtesy-message"),
                Static("", classes="spacer"),
                Container(
                    Button("Start Monitoring", id="start_monitoring"),
                    Button("Settings", id="settings"),
                    Button("Exit", id="exit"),
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
            try:
                print("Loading config...")
                config = ConfigManager.load_config()
                if os.path.exists(config["inventory_path"]):
                    print("Parsing inventory...")
                    groups = InventoryParser.parse_inventory(config["inventory_path"])
                    print(f"Found {len(groups)} groups")
                    self.app.push_screen(TableScreen(groups))
                else:
                    print("No inventory file found")
            except Exception as e:
                print(f"Error starting monitoring: {e}")

class PingPanel(App):
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

    Button {
        margin: 0 1;
        min-width: 16;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield MainMenu()
        yield Footer()

    def on_mount(self) -> None:
        # Disable focusing for all widgets recursively
        for widget in self.query("*"):
            if hasattr(widget, 'can_focus'):
                widget.can_focus = False
        
        # Push the main menu screen
        self.push_screen(MainMenu())

if __name__ == "__main__":
    app = PingPanel()
    app.run()

### Copyright CrabMan Stan

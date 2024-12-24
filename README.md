# PingPanel
###  TUI Uptime Checker
 Do you manage large infrastructure?
 
 😴 Are you sick of entering device information into PRTG and other Uptime monitors? 😴
 
 🖥️ Do you long for the days when everything was in the terminal? 🖥️

🧘‍♂️ Do you want to just point your monitor to an inventory file and let it do the rest? 🧘‍♂️
### Introducing PingPanel
##### PingPanel is a simple, ping only indicator of if your host is alive or not, it doesn't tell you the device temperature, doesn't support SNMP and definitely DOES NOT COME WITH ANY WARRANTY
##### Here's a picure of the main monitoring screen:

![Demo Screen](https://github.com/user-attachments/assets/8acb19da-d42f-4377-afd1-d9fa21535309)

##### PingPanel works by you entering in your YAML formatted Ansible inventory, and then sorts the hosts into a tree structure, from it logs (according to your specifications) via ping how long the host is up for.
##### PingPanel will tell you if there is a status change of a deivce in the top left, and give you some numbers of how many are up/down.
##### PingPanel logs the uptime of devices in a current and historical log, aggregating the uptime into an hourly average to save on processing.

It is entirely written in python, and uses Textual to give you a nice looking, custom-themed UI all from the terminal, and under 30KB
You can currently modify:

```
Ping parameters: Check interval / Maximum Acceptable Latency / Ping Packet Count / Maximum Concurrent Ping Threads
Inventory: Location of you Ansible inventory
```

If you need to make an Ansible Inventory for your Tailscale Network then feel free to use my tool AnsiScale: https://github.com/xkz0/ansiscale

If not then you need to have an inventory structured similar to this:

```
# Ansible inventory generated from Tailscale status
---
client_machines:
  children:
    cameras:
      hosts:
        "Camera2 camera2.example.com":
          ansible_host: camera2.example.com
        "Camera1 camera1.example.com":
          ansible_host: camera1.example.com
    clients:
      hosts:
        "mediabox1 mediabox1.example.com":
          ansible_host: mediabox1.example.com
        "laptop1 laptop1.example.com":
          ansible_host: laptop1.example.com
        "tablet1 tablet1.example.com":
          ansible_host: tablet1.example.com
        "phone1 phone1.example.com":
          ansible_host: phone1.example.com
        "camera3 camera3.example.com":
          ansible_host: camera3.example.com
        "raspi1 raspi1.example.com":
          ansible_host: raspi1.example.com
        "desktop1 desktop1.example.com":
          ansible_host: desktop1.example.com
        "laptop2 laptop2.example.com":
          ansible_host: laptop2.example.com
server:
  children:
    tag:servers:
      hosts:
        "nas1 nas1.example.com":
          ansible_host: nas1.example.com
        "nas2 nas2.example.com":
          ansible_host: nas2.example.com
        "testserver1 testserver1.example.com":
          ansible_host: testserver1.example.com
unknown:
  hosts:
    "desktop2 desktop2.example.com":
      ansible_host: desktop2.example.com
    "desktop3 desktop3.example.com":
      ansible_host: desktop3.example.com
```

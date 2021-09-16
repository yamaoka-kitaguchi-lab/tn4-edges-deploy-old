#!/usr/bin/env python3
from pprint import pprint
import os
import sys
import json
import yaml

from urllib.parse import urlencode
import requests

from ansible.constants import DEFAULT_VAULT_ID_MATCH
from ansible.parsing.vault import VaultLib
from ansible.parsing.vault import VaultSecret
from ansible.parsing.vault import AnsibleVaultError

from migrate import load as migration_rule_load
from devices import load as device_load
from vlans import load as vlan_load
from interfaces import load_chassis_interfaces as chassis_interface_load

VAULT_FILE = os.path.join(os.path.dirname(__file__), "../inventories/production/group_vars/all/vault.yml")
VAULT_PASSWORD_FILE = os.path.join(os.path.dirname(__file__), "../.secrets/vault-pass.txt")


class NetBoxClient:
  def __init__(self, netbox_url, netbox_api_token):
    self.api_endpoint = netbox_url.rstrip("/") + "/api"
    self.token = netbox_api_token


  def query(self, request_path, data=None, update=False):
    headers = {
      "Authorization": f"Token {self.token}",
      "Content-Type": "application/json",
      "Accept": "application/json; indent=4"
    }
    responses = []
    url = self.api_endpoint + request_path

    if data:
      cnt, limit = 0, 100
      while cnt < len(data):
        d = data[cnt:cnt+limit]
        raw = None
        if update:
          raw = requests.patch(url, json.dumps(d), headers=headers, verify=True)
        else:
          raw = requests.post(url, json.dumps(d), headers=headers, verify=True)
        responses += json.loads(raw.text)
        cnt += limit

    else:
      while url:
        raw = requests.get(url, headers=headers, verify=True)
        res = json.loads(raw.text)
        responses += res["results"]
        url = res["next"]

    return responses


  def get_all_vlans(self):
    return self.query("/ipam/vlans/")


  def get_all_vids(self):
    vlans = self.get_all_vlans()
    return [vlan["vid"] for vlan in vlans]


  def get_vlan_resolve_hints(self):
    hints = {}
    for vlan in self.get_all_vlans():
      hints[vlan["vid"]] = vlan["id"]
    return hints


  def make_vlan_resolver(self):
    hints = self.get_vlan_resolve_hints()
    def resolver(vid):
      try:
        return hints[vid]
      except KeyError:
        return None
    return resolver


  def get_all_ipaddresses(self):
    return self.query("/ipam/ip-addresses/")


  def get_all_ips(self):
    ipaddrs = self.get_all_ipaddresses()
    return [ipaddr["address"] for ipaddr in ipaddrs]


  def get_ip_resolve_hint(self):
    hints = {}
    for ip in self.get_all_ipaddresses():
      hints[ip["address"]] = ip["id"]
    return hints


  def get_all_sitegroups(self):
    return self.query("/dcim/site-groups/")


  def get_all_sitegroupslugs(self):
    sitegroups = self.get_all_sitegroups()
    return [sitegroup["slug"] for sitegroup in sitegroups]


  def get_all_regions(self):
    return self.query("/dcim/regions/")


  def get_all_regionsslug(self):
    regions = self.get_all_regions()
    return [region["slug"] for region in regions]


  def get_all_sites(self):
    self.all_sites = self.query("/dcim/sites/")
    return self.all_sites


  def get_all_siteslugs(self):
    sites = self.get_all_sites()
    return [site["slug"] for site in sites]


  def get_all_devices(self):
    self.all_devices = self.query("/dcim/devices/")
    return self.all_devices
  
  
  def lookup_sitegroup(self, site_slug):
    for site in self.get_all_sites():
      if site["slug"] == site_slug:
        return site["group"]["slug"]
    print(f"Unknown site: {site_slug}")
    return None
  
  
  def lookup_region(self, site_slug):
    for site in self.get_all_sites():
      if site["slug"] == site_slug:
        return site["region"]["slug"]
    print(f"Unknown site: {site_slug}")
    return None


  def get_all_devicenames(self):
    return [device["name"] for device in self.get_all_devices()]


  def get_device_resolve_hint(self):
    hints = {}
    for device in self.get_all_devices():
      hints[device["name"]] = device["id"]
    return hints


  def get_all_interfaces(self):
    self.all_interfaces = self.query("/dcim/interfaces/")
    return self.all_interfaces


  def get_interface(self, iid):
    for interface in self.get_all_interfaces():
      if interface["id"] == iid:
        return interface


  def get_interface_resolve_hint(self):
    hints = {}
    for interface in self.get_all_interfaces():
      key = interface["device"]["name"]
      subkey = interface["name"]
      iid = interface["id"]
      try:
        hints[key][subkey] = iid
      except KeyError:
        hints[key] = {subkey: iid}
    return hints
  

  def get_mgmt_vlanid_resolve_hint(self):
    hints = {}
    for device in self.get_all_devices():
      if device["device_role"]["slug"] != "edge-sw":
        continue
      area = self.lookup_sitegroup(device["site"]["slug"])
      if area in ["ookayama-n", "ookayama-w", "midorigaoka"]:
        hints[device["name"]] = 360
      elif area in ["ookayama-e", "ookayama-s", "ishikawadai", "tamachi"]:
        hints[device["name"]] = 361
      else:
        hints[device["name"]] = 362
    return hints
  

  def get_tokyotech_vlanid_resolve_hint(self):
    hints = {}
    for device in self.get_all_devices():
      if device["device_role"]["slug"] != "edge-sw":
        continue
      region = self.lookup_region(device["site"]["slug"])
      if region in ["ookayama", "tamachi"]:
        hints[device["name"]] = 112
      else:
        hints[device["name"]] = 113
    return hints


  def get_all_vcs(self):
    return self.query("/dcim/virtual-chassis/")


  def get_all_vcnames(self):
    vcs = self.get_all_vcs()
    return [vc["name"] for vc in vcs]


  def create_vlans(self, vlans):
    existed_vids = self.get_all_vids()
    data = [
      {
        "vid": vid,
        "name": prop["name"],
        "status": "active",
        "description": prop["description"]
      }
      for vid, prop in vlans.items() if vid not in existed_vids
    ]
    data = list({v["vid"]:v for v in data}.values())
    if data:
      return self.query("/ipam/vlans/", data)
    return


  def create_sitegroups(self, sitegroups):
    existed_sitegroups = self.get_all_sitegroupslugs()
    data = [
      {
        "name": site["sitegroup_name"],
        "slug": site["sitegroup"]
      }
      for site in sitegroups if site["sitegroup"] not in existed_sitegroups
    ]
    data = list({v["slug"]:v for v in data}.values())
    if data:
      return self.query("/dcim/site-groups/", data)
    return


  def create_sites(self, sites):
    existed_sites = self.get_all_siteslugs()
    data = [
      {
        "name": site["site_name"],
        "slug": site["site"],
        "region": {"slug": site["region"]},
        "group": {"slug": site["sitegroup"]},
        "status": "active",
      }
      for site in sites if site["site"] not in existed_sites
    ]
    data = list({v["slug"]:v for v in data}.values())
    if data:
      return self.query("/dcim/sites/", data)
    return


  def create_devices(self, devices, n_stacked):
    exist_devices = self.get_all_devicenames()
    data = []
    for device in devices:
      device_name = device["name"]
      if device_name in exist_devices:
        continue
      device_type = device["device_type"]
      exceptions = ["nishi8w", "nishi8e", "r1", "b2"]
      if n_stacked[device_name] > 1 and device_name not in exceptions:
        device_type += "-st" + str(n_stacked[device_name])
      data.append({
        "name": device_name,
        "device_role": {"slug": "edge-sw"},
        "device_type": {"slug": device_type},
        "region": {"slug": device["region"]},
        "site": {"slug": device["site"]},
        "status": "active"
      })
    data = list({v["name"]:v for v in data}.values())
    #pprint(data)
    if data:
      return self.query("/dcim/devices/", data)
    return


  def create_and_assign_device_ips(self, devices):
    existed_ips = self.get_all_ips()
    interface_hints = self.get_interface_resolve_hint()
    data = [
      {
        "address": "/".join([device["ipv4"], device["cidr"]]),
        "status": "active",
        "dns_name": ".".join([device["name"], "m.noc.titech.ac.jp"]),
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": interface_hints[device["name"]]["irb"],
        "assigned_object": {
          "id": interface_hints[device["name"]]["irb"]
        },
      }
      for device in devices if "/".join([device["ipv4"], device["cidr"]]) not in existed_ips
    ]
    if data:
      return self.query("/ipam/ip-addresses/", data)
    return


  def set_primary_device_ips(self, devices):
    ip_hints = self.get_ip_resolve_hint()
    device_hints = self.get_device_resolve_hint()
    data = [
      {
        "id": device_hints[device["name"]],
        "primary_ip4": ip_hints["/".join([device["ipv4"], device["cidr"]])],
      }
      for device in devices
    ]
    if data:
      return self.query("/dcim/devices/", data, update=True)
    return


  def create_lag_interfaces(self, lags):
    interface_hints = self.get_interface_resolve_hint()
    data = []
    for hostname, device_lags in lags.items():
      for ifname, _ in device_lags.items():
        if ifname in interface_hints[hostname].keys():
          continue
        req = {
          "device": {"name": hostname},
          "name": ifname,
          "type": "lag"
        }
        data.append(req)
    if data:
      return self.query("/dcim/interfaces/", data)


  def disable_all_interfaces(self, devices):
    interface_hints = self.get_interface_resolve_hint()
    data = []
    for hostname in [v["name"] for v in devices]:
      for ifname, iid in interface_hints[hostname].items():
        if ifname == "irb":
          continue
        req = {
          "id": iid,
          "enabled": False,
        }
        data.append(req)
    if data:
      return self.query("/dcim/interfaces/", data, update=True)


  def update_interface_configs(self, interfaces):
    interface_hints = self.get_interface_resolve_hint()
    vlan_resolver = self.make_vlan_resolver()
    mgmt_vlanid_hints = self.get_mgmt_vlanid_resolve_hint()
    tokyotech_vlanid_hints = self.get_tokyotech_vlanid_resolve_hint()
    data = []
    orphan_vlans = {}

    for hostname, device_interfaces in interfaces.items():
      orphan_vlans[hostname] = []
      for interface, props in device_interfaces.items():
        req = {
          "id": interface_hints[hostname][interface],
          "description": props["description"],
          "enabled": props["enabled"],
          "tags": [],
        }

        # Enable PoE feature on specified interfaces
        if props["poe"]:
          req["tags"].append({"slug": "poe"})

        # LAG
        if props["lag"]:
          req["lag"] = {
            "device": {"name": hostname},
            "name": props["lag"]
          }

        # Configure untagged VLAN based on the properties (mode: ACCESS)
        if props["mode"] == "ACCESS":
          vid = vlan_resolver(props["untagged"])  # Convert VLAN ID to NetBox VLAN UNIQUE ID
          if vid is None:
            orphan_vlans[hostname].append(props["untagged"])
          else:
            req["mode"] = "access"
            req["untagged_vlan"] = vid

        # Configure tagged VLAN based on the properties (mode: TRUNK)
        if props["mode"] == "TRUNK":
          vids = []
          for vlanid in props["tagged"]:
            vid = vlan_resolver(vlanid)
            if vid is None:
              orphan_vlans[hostname].append(vlanid)
            else:
              vids.append(vid)
          if vids:
            req["mode"] = "tagged"
            req["tagged_vlans"] = vids
            
        # Configure uplink (mode: UPLINK)
        if props["mode"] == "UPLINK":
          req["mode"] = "tagged-all"
          req["tags"].append({"slug": "uplink"})

        # Configure Wi-Fi (mode: WIFI)
        if props["mode"] == "WIFI":
          tokyotech = tokyotech_vlanid_hints[hostname]
          eduroam = 169
          event_net = [161, 162, 163, 164, 171, 173]
          wi2 = [341, 342]
          req["mode"] = "tagged"
          req["untagged_vlan"] = vlan_resolver(mgmt_vlanid_hints[hostname])
          req["tagged_vlans"] = [
            vlan_resolver(vid) for vid in [tokyotech, eduroam, *event_net, *wi2]
          ]
          req["tags"].append({"slug": "wifi"})

        data.append(req)

    if orphan_vlans:
      with open(os.path.join(os.path.dirname(__file__), "./orphan-vlans.json"), "w") as fd:
        json.dump(orphan_vlans, fd, indent=2)
    if data:
      return self.query("/dcim/interfaces/", data, update=True)
    return


def __load_encrypted_secrets():
  with open(VAULT_FILE) as v, open(VAULT_PASSWORD_FILE, "r") as p:
    key = str.encode(p.read().rstrip())
    try:
      vault = VaultLib([(DEFAULT_VAULT_ID_MATCH, VaultSecret(key))])
      raw = vault.decrypt(v.read())
      return yaml.load(raw, Loader=yaml.CLoader)
    except AnsibleVaultError as e:
      print("Failed to decrypt the vault. Check your password and try again:", e, file=sys.stderr)
      sys.exit(1)


def migrate_edge(rule, tn3_interfaces, tn4_hostname):
  tn4_interfaces = {}
  tn4_lag_interfaces = {}
  summary = []
  ok = True
  for tn4_port, rule_props in rule.items():
    
    # Uplink ports
    if rule_props["uplink_mode"]:
      if tn4_port[:2] == "ae":
        if tn4_port not in tn4_lag_interfaces.keys():
          cf = {
            "enabled":     True,
            "description": rule_props["description"],
            "mode":        "UPLINK",
            "poe":         False,
            "lag":         None,
          }
          tn4_interfaces[tn4_port] = cf
          tn4_lag_interfaces[tn4_port] = cf
        continue
      
      parent = rule_props["lag"]
      if parent:
        tn4_interfaces[tn4_port] = {
          "enabled":     True,
          "description": rule_props["description"],
          "mode":        "NONE",
          "poe":         False,
          "lag":         parent,
        }

      else:
        tn4_interfaces[tn4_port] = {
          "enabled":     True,
          "description": rule_props["description"],
          "mode":        "UPLINK",
          "poe":         False,
          "lag":         None,
        }
        
    # Wi-Fi ports
    elif rule_props["wifi_mode"]:

      # To Meraki switch: create LAG interface
      if tn4_port[:2] == "ae":
        if tn4_port not in tn4_lag_interfaces.keys():
          cf = {
            "enabled":     rule_props["enable"],
            "description": rule_props["description"],
            "mode":        "WIFI",
            "poe":         False,
            "lag":         None,
          }
          tn4_interfaces[tn4_port] = cf
          tn4_lag_interfaces[tn4_port] = cf
        continue

      # To Meraki switch: append child interfaces to the LAG
      parent = rule_props["lag"]
      if parent:
        tn4_interfaces[tn4_port] = {
          "enabled":     True,
          "description": rule_props["description"],
          "mode":        "NONE",
          "poe":         False,
          "lag":         parent,
        }

      # To AP
      else:
        tn4_interfaces[tn4_port] = {
          "enabled":     rule_props["enable"],
          "description": rule_props["description"],
          "mode":        "WIFI",
          "poe":         rule_props["poe"],
          "lag":         None,
        }

    # other than Uplink/Wi-Fi ports
    else:
      tn3_port = rule_props["tn3_port"]
      try:
        tn4_interfaces[tn4_port] = tn3_interfaces[tn3_port]
      except KeyError as e:
        ok = False
        if rule_props["lag"]:
          ok = True
        if not ok:
          print(f"No interface found on Tn3 ({tn4_hostname}): from {tn3_port} to {tn4_port}")
        continue

      for override in ["description", "enable", "poe", "lag"]:
        tn4_interfaces[tn4_port][override] = rule_props[override]

      summary.append({
        "from": tn3_port,
        "to": tn4_port,
        "description": rule_props["description"],
      })

  summary.sort(key=lambda x: x["from"])
  return ok, tn4_interfaces, tn4_lag_interfaces, summary


def migrate_all_edges(devices, tn3_interface_info, tn3_stack_info, hosts=[]):
  tn4_all_interfaces = {}
  tn4_all_lag_interfaces = {}
  tn4_all_n_stacked = {}
  migration_results = {}
  migration_rules = migration_rule_load(hosts=hosts)

  for device in devices:
    tn4_hostname = device["name"]
    if hosts and tn4_hostname not in hosts:
      continue
    tn3_hostname = device["tn3_name"]
    tn3_interfaces = tn3_interface_info[tn3_hostname]
    tn3_n_stacked = tn3_stack_info[tn3_hostname]
    rule = migration_rules[tn4_hostname]

    ok, tn4_interfaces, tn4_lag_interfaces, summary = migrate_edge(rule, tn3_interfaces, tn4_hostname)
    if ok:
      tn4_all_interfaces[tn4_hostname] = tn4_interfaces
      tn4_all_lag_interfaces[tn4_hostname] = tn4_lag_interfaces
      tn4_all_n_stacked[tn4_hostname] = tn3_n_stacked
      migration_results[tn4_hostname] = summary

  with open(os.path.join(os.path.dirname(__file__), "./port-migration.json"), "w") as fd:
    json.dump(migration_results, fd, indent=2)
  return tn4_all_interfaces, tn4_all_lag_interfaces, tn4_all_n_stacked


def main():
  secrets = __load_encrypted_secrets()
  nb = NetBoxClient(secrets["netbox_url"], secrets["netbox_api_token"])
  
  vlans = vlan_load()
  devices = device_load(hosts=[])
  hosts = [d["name"] for d in devices]
  sitegroups = [{k: d[k] for k in ["sitegroup_name", "sitegroup"]} for d in devices]
  sites = [{k: d[k] for k in ["region", "sitegroup", "site_name", "site"]} for d in devices]
  tn3_interfaces, tn3_n_stacked = chassis_interface_load()
  tn4_interfaces, tn4_lags, tn4_n_stacked = migrate_all_edges(devices, tn3_interfaces, tn3_n_stacked, hosts=hosts)

  #print("STEP 1 of 9: Create VLANs")
  #res = nb.create_vlans(vlans)
  #if res:
  #  pprint(res)

  #print("STEP 2 of 9: Create Site Groups")
  #res = nb.create_sitegroups(sitegroups)
  #if res:
  #  pprint(res)

  #print("STEP 3 of 9: Create Sites")
  #res = nb.create_sites(sites)
  #if res:
  #  pprint(res)

  print("STEP 4 of 9: Create Devices")
  res = nb.create_devices(devices, tn4_n_stacked)
  if res:
    pprint(res)

  print("STEP 5 of 9: Create IP Addresses")
  res = nb.create_and_assign_device_ips(devices)
  if res:
    pprint(res)

  print("STEP 6 of 9: Update device addresses")
  res = nb.set_primary_device_ips(devices)
  if res:
    pprint(res)

  print("STEP 7 of 9: Create LAG interfaces")
  res = nb.create_lag_interfaces(tn4_lags)
  if res:
    pprint(res)

  print("STEP 8 of 9: Disable all interfaces")
  res = nb.disable_all_interfaces(devices)
  if res:
    pprint(res)

  print("STEP 9 of 9: Update interface configurations")
  res = nb.update_interface_configs(tn4_interfaces)
  if res:
    pprint(res)


def develop():
  secrets = __load_encrypted_secrets()
  nb = NetBoxClient(secrets["netbox_url"], secrets["netbox_api_token"])


if __name__ == "__main__":
    main()
    #develop()

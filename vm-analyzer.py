#!/usr/bin/env python3

import datetime
import guestfs
import json
import logging
import os
import re
import signal
import subprocess
import ssl
import sys
import time
import uuid
import threading
import requests

from requests.auth import HTTPDigestAuth

from pyVmomi import vim
from pyVim.connect import SmartStubAdapter, VimSessionOrientedStub, Disconnect
from pyVim.task import WaitForTask

from flask import Flask, request, jsonify
from flask_restful import Resource, Api, reqparse

MANIFEST = {
    "files": [
        { "path": "/etc/*.conf", "collect_content": False },
        { "path": "/etc/hosts", "collect_content": False },
        { "path": "/etc/redhat-access-insights/machine-id", "collect_content": False },
        { "path": "c:/windows/system32/*.scr", "collect_content": False },
        { "path": "c:/windows/system32/msi*.*", "collect_content": False },
        { "path": "c:/windows/system32/netapi32.dll", "collect_content": False },
        { "path": "C://Program Files/Microsoft SQL Server/110", "collect_content": False },
        { "path": "C://Program Files/Microsoft SQL Server/120", "collect_content": False },
        { "path": "C://Program Files/Microsoft SQL Server/130", "collect_content": False },
        { "path": "C://Program Files/Microsoft SQL Server/140", "collect_content": False },
        { "path": "C://Program Files/IBM/WebSphere/AppServer", "collect_content": False },
        { "path": "/etc/group", "collect_content": False },
        { "path": "/etc/oraInst.loc", "collect_content": True },
        { "path": "/u01/app/oraInventory", "collect_content": True },
        { "path": "/opt/mssql/bin/mssql-conf", "collect_content": False },
        { "path": "/usr/sap/hostctrl/exe/saphostctrl", "collect_content": False },
        { "path": "/etc/.ibm/registry/InstallationManager.dat", "collect_content": False}
    ]
}

class ConcurrentScan(threading.Thread):
  
    def __init__(self, post_body):
        threading.Thread.__init__(self)
        self._request = post_body
        print("Initializing ConcurrentScan")
        
  
    def run(self):
        vm_config = VmAnalyzer(self._request).get_vm_config()
        print("VM Config: ")
        print("\tMountpoints: %s" % vm_config["software"][0]["mountpoints"])
        print("\tOperating System: %s" % vm_config["software"][0]["name"])
        print("\tHostname: %s" % vm_config["software"][0]["hostname"])
        for file in vm_config["software"][0]["files"]:
            if file["name"] == '/etc/oraInst.loc':
                print("/etc/oraInst.loc: %s" % file["content"])
            if file["name"] == '/u01/app/oraInventory':
                print("/u01/app/oraInventory: %s" % file["content"])
        
class VmAnalyzer:
    def __init__(self, post_body):
        now = datetime.datetime.now()
        self._request = post_body
        print("Initializing VmAnalyzer at %s" % now.strftime("%Y-%m-%d %H:%M:%S"))
        self._inventory_db = self._get_inventory_db()
        self._vm_uuid = self._get_vm_uuid()
        self._vm_host = self._get_vm_host()
        self._service_instance = self._connect()
        self._vm = self._find_vm_by_uuid()
        self._snapshot_name = "%s-vm-analysis" % now.strftime("%Y%m%d%H%M%S")
        self._snapshot_desc = "%s - VM Analysis" % now.strftime("%Y-%m-%d %H:%M:%S")
        self._snapshot = None

        if not os.path.exists("/tmp/%s" % self._vm_uuid):
            os.mkdir("/tmp/%s" % self._vm_uuid)
            

    def __del__(self):
        now = datetime.datetime.now()
        self._remove_snapshot()
        self._disconnect()
        print("Terminating VmAnalyzer at %s" % now.strftime("%Y-%m-%d %H:%M:%S"))
        

    def _connect(self):
        print("Connecting to %s as %s" % (self._vm_host["name"], self._request["host_authentication"]["username"]))
        smart_stub = SmartStubAdapter(
            host = self._vm_host["name"],
            port = 443,
            sslContext = ssl._create_unverified_context(),
            connectionPoolTimeout = 0
        )
        self._session_stub = VimSessionOrientedStub(
            smart_stub,
            VimSessionOrientedStub.makeUserLoginMethod(
                self._request["host_authentication"]["username"],
                self._request["host_authentication"]["password"]
            )
        )
        si = vim.ServiceInstance('ServiceInstance', self._session_stub)

        if not si:
            raise Exception("Could not connect to %s" % self._vm_host["name"])

        return si
      

    def _disconnect(self):
        try:
            Disconnect(self._service_instance)
        except:
            pass
          
          
    def _get_inventory_db(self):
        inventory_hostname = os.environ["INVENTORY_SERVICE"] + "." + os.environ["POD_NAMESPACE"] + ".svc.cluster.local"
        inventory_socket   = inventory_hostname + ":" + os.environ["FORKLIFT_INVENTORY_SERVICE_PORT"]
        inventory_db       = "https://" + inventory_socket + "/providers/vsphere/" + self._request["provider"]["uid"]
        return inventory_db
      
      
    def _call_inventory_db(self, href_slug):
        api_response = requests.get(self._inventory_db + href_slug, verify=os.environ["CA_TLS_CERTIFICATE"])
        if(api_response.ok):
            return json.loads(api_response.content)
        else:
            raise Exception("Failed call to inventory database, return code: %s" % api_response)
          

    def _get_vm_uuid(self):
        print("Looking for UUID for virtual machine with MORef: %s" % self._request["vm"]["moref"])
        href_slug = "/vms/" + self._request["vm"]["moref"]
        return self._call_inventory_db(href_slug)["uuid"]
      
      
    def _get_vm_host(self):
        print("Looking for host for virtual machine with MORef: %s" % self._request["vm"]["moref"])
        vm_href_slug = "/vms/" + self._request["vm"]["moref"]
        host_href_slug = "/hosts/" + self._call_inventory_db(vm_href_slug)["host"]["id"]
        return self._call_inventory_db(host_href_slug)
      

    def _find_vm_by_uuid(self):
        print("Looking for virtual machine with UUID '%s'" % self._vm_uuid)
        vm = self._service_instance.content.searchIndex.FindByUuid(uuid=self._vm_uuid, vmSearch=True, instanceUuid=False)
        return vm
      
      
    def _create_snapshot(self):
        print("Creating snapshot to protect the VM disks")
        task = self._vm.CreateSnapshot(name = self._snapshot_name,
                                 description = self._snapshot_desc,
                                 memory = False,
                                 # The `quiesce` parameter can be False to
                                 # make it slightly faster, but it should
                                 # be first tested independently.
                                 quiesce = True)
        WaitForTask(task)
        # Update the VM data
        self._vm.Reload()
        self._snapshot = self._vm.snapshot.currentSnapshot
        

    def _remove_snapshot(self):
        print("Removing snapshot")
        if self._snapshot:
            WaitForTask(self._snapshot.RemoveSnapshot_Task(False))
            

    def _path_win2lin(self, path):
        if not re.compile('^/').match(path):
            path = re.sub('^.*/', '/', path)
        return path
      

    def _get_vm_disks(self):
        host = self._vm.runtime.host
        print("Getting VM disk details")
        href_slug = "/vms/" + self._request["vm"]["moref"]
        return self._call_inventory_db(href_slug)["disks"]


    def _get_vm_software(self, vm_disks):
        self._create_snapshot()
        print("Snapshot MORef: %s" % self._snapshot._moId)

        nbdkit_env = os.environ.copy()
        sockets_paths = []
        nbd_servers = []
        for index, disk in enumerate(vm_disks):
            socket_path = "/tmp/%s/%s.sock" % (self._vm_uuid, "d%0.5d" % index)
            nbdkit_env = { 'LD_LIBRARY_PATH': '/opt/vmware-vix-disklib-distrib/lib64' }
            nbdkit_cmd = ['/usr/sbin/nbdkit', '--readonly', '--exit-with-parent', '--newstyle']
            nbdkit_cmd.extend(['--unix', socket_path])
            nbdkit_cmd.extend(['vddk', 'libdir=/opt/vmware-vix-disklib-distrib'])
            nbdkit_cmd.extend(['server=%s' % self._vm_host["name"]])
            nbdkit_cmd.extend(['user=%s' % self._request["host_authentication"]["username"]])
            nbdkit_cmd.extend(['password=%s' % self._request["host_authentication"]["password"]])
            nbdkit_cmd.extend(['thumbprint=%s' % self._vm_host["thumbprint"]])
            nbdkit_cmd.extend(['file=%s' % disk["file"]])
            nbdkit_cmd.extend(['vm=moref=%s' % self._vm._moId])
            nbdkit_cmd.extend(['snapshot=%s' % self._snapshot._moId])
            nbd_server = subprocess.Popen(nbdkit_cmd, env=nbdkit_env)

            # Allowing some time for the socket to be created
            for i in range(10):
                if os.path.exists(socket_path):
                    break
                time.sleep(1)

            sockets_paths.append(socket_path)
            nbd_servers.append(nbd_server)

        try:
            g = guestfs.GuestFS(python_return_dict=True)
            g.set_backend("direct")
            for socket_path in sockets_paths:
                g.add_drive_opts("", protocol="nbd", format="raw", server=["unix:%s" % socket_path], readonly=1)
            g.launch()

            roots = g.inspect_os()
            if len(roots) == 0:
                raise(Error("inspect_os: no operating systems found"))

            operating_systems = []
            for root in roots:
                osh = {}
                #osh["filesystems"] = g.inspect_get_filesystems(root)
                osh["mountpoints"] = g.inspect_get_mountpoints(root)
                osh["name"] = g.inspect_get_product_name(root)
                osh["major_version"] = g.inspect_get_major_version(root)
                osh["minor_version"] = g.inspect_get_minor_version(root)
                osh["type"] = g.inspect_get_type(root)
                osh["distro"] = g.inspect_get_distro(root)
                osh["arch"] = g.inspect_get_arch(root)
                osh["product_variant"] = g.inspect_get_product_variant(root)
                osh["package_format"] = g.inspect_get_package_format(root)
                osh["package_management"] = g.inspect_get_package_management(root)
                osh["hostname"] = g.inspect_get_hostname(root)

                for device, mp in sorted(osh["mountpoints"].items(), key=lambda k: len(k[0])):
                    try:
                        g.mount_ro(mp, device)
                    except RuntimeError as err:
                        raise err

                osh["packages"] = g.inspect_list_applications2(root)

                # with open("/data/manifest.json") as f:
                #     manifest = json.load(f)
                manifest = MANIFEST
                
                ext_ap_files = []
                for ap_file in manifest["files"]:
                    if '*' in ap_file["path"]:
                        #print("%s is a wildcard. Extending" % ap_file["path"])
                        founds = g.find(os.path.dirname(self._path_win2lin(ap_file["path"])))
                        for f in founds:
                            if re.compile(ap_file["path"]).match(f):
                                ext_ap_files.append({ "path": f, "collect_content": ap_file["collect_content"]})
                    else:
                        #print("%s is NOT a wildcard. Adding" % ap_file["path"])
                        ext_ap_files.append(ap_file)
                #print("Extended AP Files: %s" % ext_ap_files)
                
                osh["files"] = []
                for ap_file in ext_ap_files:
                    path = self._path_win2lin(ap_file["path"])
                    # Skip files that don't exist
                    if not g.is_file_opts(self._path_win2lin(path), followsymlinks=True):
                        #print("%s doesn't exist. Skipping" % ap_file["path"])
                        continue
                
                    # Collect the content of the file is requested
                    if ap_file["collect_content"]:
                        content = "\n".join(g.read_lines(path))
                    else:
                        content = None
                
                    osh["files"].append({ "name": ap_file["path"], "content": content})

                g.umount_all()
                operating_systems.append(osh)
            return operating_systems

        except Exception as e:
            os._exit()
            print("[ERROR] %s" % e)
            raise e
        finally:
            for nbd_server in nbd_servers:
                nbd_server.kill()
            for socket_path in sockets_paths:
                os.remove(socket_path)


    def get_vm_config(self):
        vm_disks = self._get_vm_disks()
        vm_config = {
            "disks": vm_disks,
            "software": self._get_vm_software(vm_disks),
        }
        return vm_config
      

class Scanning(Resource):
    def post(self):
        post_body = request.get_json()      
        scan = ConcurrentScan(post_body)
        scan.start()
        return "Scan started for VM MORef: " + post_body["vm"]["moref"]
      

class Debug(Resource):
    def get(self): 
        return "<h1>Debug</h1><p>Working</p>"
      

def main():     
    app = Flask(__name__)
    api = Api(app)
    api.add_resource(Scanning, '/scan')
    api.add_resource(Debug, '/debug')
    app.run(host= '0.0.0.0')
    

if __name__ == '__main__':
    main()



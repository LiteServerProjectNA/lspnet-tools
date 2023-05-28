import json
import subprocess
from datetime import datetime, timedelta
import ipaddress
from typing import Dict

from config_types import CommonOSPFConfig, InterfaceConfig, ConnectorPhantunClientConfig, ConnectorPhantunServerConfig
from get_logger import get_logger

logger = get_logger('app')
GIT_VERSION = subprocess.check_output(["git", "rev-parse", "--verify", "HEAD"], encoding='utf-8').strip()


def load_or_create_keys(namespace, name):
    try:
        with open('local/{}.{}.json'.format(namespace, name)) as f:
            content = f.read()
        data =  json.loads(content)
        new_key = data['private']
        new_pub = subprocess.check_output(["wg", "pubkey"], encoding='utf-8', input=new_key).strip()
        pub_key = data.get('public', '')
        if pub_key and pub_key != new_pub:
            logger.warning('wireguard public key does not match private key! name: {}'.format(name))
        return {
            "private": new_key,
            "public": new_pub,
        }
    except FileNotFoundError:
        new_key = subprocess.check_output(["wg", "genkey"], encoding='utf-8').strip()
        new_pub = subprocess.check_output(["wg", "pubkey"], encoding='utf-8', input=new_key).strip()
        data = {
            "private": new_key,
            "public": new_pub,
        }
        with open('local/{}.{}.json'.format(namespace, name), 'w') as f:
            f.write(json.dumps(data, ensure_ascii=False))
        return data


def get_bird_config(router_id, direct_interface_names, ospf_exclude_import_cidrs, ospf_exclude_export_cidrs, ospf_area_config):
    current_time_text = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    current_time_for_auth_text = (datetime.now() - timedelta(1)).strftime('%d-%m-%Y %H:%M:%S')

    router_id_text = 'router id {};'.format(router_id) if router_id else ''
    dnames_text = '\n'.join(['interface "{}";'.format(name) for name in direct_interface_names])
    localnet_no_import_variable_text = 'define LOCALNET_NO_IMPORTSET=[{}];'.format(','.join(ospf_exclude_import_cidrs)) if ospf_exclude_import_cidrs else ''
    localnet_no_export_variable_text = 'define LOCALNET_NO_EXPORTSET=[{}];'.format(','.join(ospf_exclude_export_cidrs)) if ospf_exclude_export_cidrs else ''
    import_filter_text = '''import filter {{
if net !~ LOCALNET_NO_IMPORTSET then accept;
else reject;
}}''' if localnet_no_import_variable_text else 'import all'
    export_filter_text = '''export filter {{
if net !~ LOCALNET_NO_EXPORTSET then accept;
else reject;
}}''' if localnet_no_export_variable_text else 'export all'

    all_area_texts = []
    for area_id, area_interface_mapping in ospf_area_config.items():
        text_parts = []
        text_parts.append(f'''area {area_id} {{''')
        for interface_name, ospf_interface_config in area_interface_mapping.items():
            text_parts.append(f'''interface "{interface_name}" {{''')
            if ospf_interface_config.cost:
                text_parts.append("cost {};".format(ospf_interface_config.cost))
            if ospf_interface_config.type:
                text_parts.append("type {};".format(ospf_interface_config.type))
            if ospf_interface_config.auth:
                text_parts.append("authentication cryptographic;")
                text_parts.append(f'''password "{ospf_interface_config}" {{
generate to "31-12-2099 23:59:59";
accept from {current_time_for_auth_text}
algorithm hmac sha512;
}};''')
            text_parts.append('};')
        text_parts.append('};')

        all_area_texts.append('\n'.join(text_parts))

    final_area_text = '\n'.join(all_area_texts)

    return f'''# Auto generated by lspnet-tools at {current_time_text}
# version: {GIT_VERSION}

{localnet_no_import_variable_text}
{localnet_no_export_variable_text}

log stderr all;
{router_id_text}
protocol device {{
    
}}
protocol direct {{
    ipv4;
    {dnames_text}
}}
protocol kernel {{
    ipv4 {{
        import none;
        export where proto = "wg";
    }};
}}
protocol ospf v2 wg {{
    ecmp yes;
    merge external yes;
    ipv4 {{
        {import_filter_text};
        {export_filter_text};
    }};
    {final_area_text}
}}
'''


class NetworkConfigParser:
    def __init__(self, root_config):
        self.hostname = root_config['hostname']
        self.namespace = root_config['namespace']
        self.ifname_prefix = root_config.get('prefix', self.hostname)
        self.router_id = root_config.get('routerid', '')

        local_config = root_config.get('local', {})
        if not local_config:
            logger.warning('no local config found, node will work in forward-mode only')
            self.enable_local_network = False
        else:
            self.enable_local_network = True
            self.local_is_exit_node = local_config.get('exit', True)
            self.local_veth_prefix = local_config.get('name', '{}-veth'.format(self.namespace))

            self.local_interface = InterfaceConfig()
            self.local_interface.address = local_config['address']
            self.local_interface.name = local_config['ethname']
            self.local_interface.enable_ospf = local_config.get('ospf', False)
            if self.local_interface.enable_ospf:
                self.local_interface.ospf_config = CommonOSPFConfig(
                    local_config.get('area', 0),
                    local_config.get('cost', 0),
                    local_config.get('auth', ''),
                    'ptp')

        network_config = root_config['config']
        self.network_default_enable_ospf = network_config.get('ospf', False)
        self.network_default_ospf_config = CommonOSPFConfig(
            network_config.get('area', 0),
            network_config.get('cost', 0),
            network_config.get('auth', ''),
            'ptp',
        )

        # Firewall
        firewall_config = root_config.get('firewall', {})
        if not firewall_config:
            logger.warn('no firewall configured. make sure you have UFW enabled or have custom rules configured!')
            self.enable_local_firewall = False
        else:
            self.enable_local_firewall = True

        # Interfaces
        network_config = root_config['networks']
        self.interfaces : Dict[str, InterfaceConfig] = {}

        for interface_name, interface_config in network_config.items():
            wg_config = load_or_create_keys(self.namespace, interface_name)
            new_interface = InterfaceConfig(
                "{}-{}".format(self.namespace, interface_name),
                wg_config['private'],
                wg_config['public'],
                interface_config.get('mtu', 1420),
                interface_config['address'],
                interface_config.get('listen', 0),
                interface_config['peer'],
                '0.0.0.0/0',
                interface_config.get('endpoint', ''),
                interface_config.get('keepalive', 25 if interface_config.get('endpoint', '') else 0),
                interface_config.get('autoconnect', False),
            )
            new_interface.enable_ospf = interface_config.get('ospf', self.network_default_enable_ospf)
            if new_interface.enable_ospf:
                new_interface.ospf_config = CommonOSPFConfig(
                    interface_config.get('area', self.network_default_ospf_config.area),
                    interface_config.get('cost', self.network_default_ospf_config.cost),
                    interface_config.get('auth', self.network_default_ospf_config.auth),
                    'ptp',
                )

            # Validation
            if not new_interface.validate():
                exit(1)

            # Connector
            new_connector = None
            if 'connector' in interface_config:
                connector_config = interface_config['connector']

                if connector_config['type'] == 'phantun-server':
                    new_connector = ConnectorPhantunServerConfig(
                        connector_config['listen'],
                        connector_config['tun-name'],
                        connector_config['tun-local'],
                        connector_config['tun-peer'],
                    )

                    if new_interface.listen == 0:
                        logger.warning('connector type [{}] requires wireguard listen-port not to be zero, a dynamic config will be generated'.format(connector_config['type']))
                        new_connector.remote = '#dynamic'
                    else:
                        new_connector.remote = '127.0.0.1:{}'.format(new_interface.listen)
                elif connector_config['type'] == 'phantun-client':
                    new_connector = ConnectorPhantunClientConfig(
                        '127.0.0.1:{}'.format(connector_config['listen']),
                        connector_config['remote'],
                        connector_config['tun-name'],
                        connector_config['tun-local'],
                        connector_config['tun-peer'],
                    )

                    if new_interface.endpoint:
                        logger.warning('interface has specified an endpoint ({}), which will be override by connector [{}]'.format(new_interface.endpoint, connector_config['type']))

                    new_interface.endpoint = '127.0.0.1:{}'.format(connector_config['listen'])
                else:
                    logger.error('unknown connector type: {}'.format(connector_config['type']))
                    exit(1)

            new_interface.connector = new_connector

            self.interfaces[new_interface.name] = new_interface

        # BIRD config
        interface_cidrs = [str(ipaddress.ip_interface(interface_item.address).network) for interface_item in self.interfaces.values() if interface_item.enable_ospf]
        if self.enable_local_network and self.local_interface.enable_ospf:
            interface_cidrs.append(str(ipaddress.ip_network(self.local_interface.address)))

        ospf_area_config = {}
        for interface_item in self.interfaces.values():
            if not interface_item.enable_ospf:
                continue

            if interface_item.ospf_config.area not in ospf_area_config:
                ospf_area_config[interface_item.ospf_config.area] = {}

            ospf_area_config[interface_item.ospf_config.area][interface_item.name] = interface_item.ospf_config

        if self.enable_local_network and self.local_interface.enable_ospf:
            if self.local_interface.ospf_config.area not in ospf_area_config:
                ospf_area_config[interface_item.ospf_config.area] = {}

            ospf_area_config[interface_item.ospf_config.area]["{}1".format(self.local_veth_prefix)] = self.local_interface.ospf_config

        self.network_bird_config = get_bird_config('', [], interface_cidrs, [], ospf_area_config)

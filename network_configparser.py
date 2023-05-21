import json
import subprocess
from datetime import datetime
import ipaddress
from get_logger import get_logger

logger = get_logger('app')


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


def get_bird_config(router_id, direct_interface_names, ospf_exclude_cidrs, ospf_areas):
    current_time_text = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    router_id_text = 'router id {};'.format(router_id) if router_id else ''
    dnames_text = '\n'.join(['interface "{}";'.format(name) for name in direct_interface_names])
    filter_expression = ' && '.join(['net !~ {}'.format(ip_cidr) for ip_cidr in ospf_exclude_cidrs])
    filter_statement_text = 'if ({}) then accept;\nelse reject;'.format(filter_expression)
    
    all_area_configs = []
    for area_id, area_config in ospf_areas.items():
        new_area = {}
        new_area['id'] = area_id
        new_area['interfaces'] = []

        area_interfaces = area_config['interfaces']
        for interface_name, area_interface_config in area_interfaces.items():
            new_interface = {}
            new_interface['name'] = interface_name
            new_interface['cost'] = area_interface_config.get('cost', 0)

            new_area['interfaces'].append(new_interface)

        all_area_configs.append(new_area)
    
    all_area_texts = []
    for area_config in all_area_configs:
        text_parts = []
        text_parts.append(f'''area {area_config['id']} {{''')
        for interface_config in area_config['interfaces']:
            text_parts.append(f'''interface {interface_config['name']} {{''')
            if interface_config['cost']:
                text_parts.append("cost {};".format(interface_config['cost']))
            text_parts.append('};')
        text_parts.append('};')

        all_area_texts.append('\n'.join(text_parts))

    final_area_text = '\n'.join(all_area_texts)

    return f'''# Auto generated by NetworkConfigParser at {current_time_text}
log syslog all;
{router_id_text}
protocol device {{
    
}}
protocol direct {{
  ipv4;
  {dnames_text}
}}
protocol kernel {{
    ipv4 {{
        export where proto = "wg";
    }};
}}
protocol ospf v2 wg {{
    ecmp yes;
    merge external yes;
    ipv4 {{
        import filter {{
            {filter_statement_text}
        }};
        export all;
    }};
    {final_area_text}
}}
'''


class NetworkConfigParser:
    def __init__(self, config):
        self.hostname = config['hostname']
        self.namespace = config['namespace']
        self.ifname_prefix = config.get('prefix', self.hostname)
        self.router_id = config.get('routerid', '')

        local_config = config.get('local', {})
        if not local_config:
            logger.warning('no local config found, node will work in forward-mode only')
            self.enable_local_network = False
        else:
            self.enable_local_network = True
            self.local_network = local_config['address']
            self.local_veth_prefix = local_config.get('name', '{}-veth'.format(self.namespace))
            self.local_enable_ospf = local_config.get('ospf', False)

        network_config = config['config']
        self.network_default_enable_ospf = network_config.get('ospf', False)
        self.network_default_ospf_area = network_config.get('area', 0)
        self.network_default_ospf_cost = network_config.get('cost', 0)

        # Interfaces
        network_config = config['networks']
        self.interfaces = {}
        for interface_name, interface_config in network_config.items():
            wg_config = load_or_create_keys(self.namespace, interface_name)
            new_interface = {}
            new_interface['private'] = wg_config['private']
            new_interface['public'] = wg_config['public']
            new_interface['mtu'] = interface_config.get('mtu', 1420)
            new_interface['address'] = interface_config['address']
            new_interface['listen'] = interface_config.get('listen', 0)
            new_interface['peer'] = interface_config['peer']
            new_interface['allowed'] = '0.0.0.0/0'
            new_interface['endpoint'] = interface_config.get('endpoint', '')
            new_interface['keepalive'] = interface_config.get('keepalive', 25 if new_interface['endpoint'] else 0)
            new_interface['ospf'] = interface_config.get('ospf', self.network_default_enable_ospf)
            new_interface['ospf_area'] = interface_config.get('area', self.network_default_ospf_area)
            new_interface['ospf_cost'] = interface_config.get('cost', self.network_default_ospf_cost)
            new_interface['autoconnect'] = interface_config.get('autoconnect', False)

            # Validation
            if new_interface['autoconnect'] and not new_interface['endpoint']:
                logger.error('autoconnect cannot be enabled without endpoint specified!')
                exit(1)

            # Connector
            new_connector = {}
            if 'connector' in interface_config:
                connector_config = interface_config['connector']

                new_connector['type'] = connector_config['type']
                if new_connector['type'] == 'phantun-server':
                    new_connector['local'] = connector_config['listen']
                    if new_interface['listen'] == 0:
                        logger.warning('connector type [{}] requires wireguard listen-port not to be zero, a dynamic config will be generated'.format(new_connector['type']))
                        new_connector['remote'] = 'dynamic#127.0.0.1:{listen}'
                    else:
                        new_connector['remote'] = '127.0.0.1:{}'.format(new_interface['listen'])
                    new_connector['tun-local'] = connector_config['tun-local']
                    new_connector['tun-peer'] = connector_config['tun-peer']
                elif new_connector['type'] == 'phantun-client':
                    new_connector['local'] = '127.0.0.1:{}'.format(connector_config['listen'])
                    new_connector['remote'] = connector_config['remote']
                    new_connector['tun-local'] = connector_config['tun-local']
                    new_connector['tun-peer'] = connector_config['tun-peer']

                    if new_interface['endpoint']:
                        logger.warning('interface has specified an endpoint ({}), which will be override by connector [{}]'.format(new_interface['point'], new_connector['type']))

                    new_interface['endpoint'] = '127.0.0.1:{}'.format(connector_config['listen'])
                else:
                    logger.error('unknown connector type: {}'.format(new_connector['type']))
                    exit(1)

            new_interface['connector'] = new_connector

            self.interfaces["{}-{}".format(self.namespace, interface_name)] = new_interface

        # BIRD config
        interface_cidrs = [str(ipaddress.ip_interface(interface_config['address']).network) for interface_name, interface_config in self.interfaces.items() if interface_config['ospf']]
        interface_ospf_info = {}
        for interface_name, interface_config in self.interfaces.items():
            if not interface_config['ospf']:
                continue
            
            if interface_config['ospf_area'] not in interface_ospf_info:
                interface_ospf_info[interface_config['ospf_area']] = {
                    "interfaces": {},
                }
            
            interface_ospf_info[interface_config['ospf_area']]["interfaces"][interface_name] = {
                'cost': interface_config['ospf_cost'],
            }

        self.network_bird_config = get_bird_config('', [], interface_cidrs, interface_ospf_info)

import html
import json
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


def parse_sliver_info(xml_text):
    root = ET.fromstring(xml_text)

    # Get experiment description
    rspec_tour = root.find(
        ".//{http://www.protogeni.net/resources/rspec/ext/apt-tour/1}description"
    )
    description = rspec_tour.text if rspec_tour is not None else "No description"

    # Get expiration
    expiration = root.get("expires", "No expiration date")

    # Parse node information
    nodes = []
    for node in root.findall(".//{http://www.geni.net/resources/rspec/3}node"):
        node_info = {
            "client_id": node.get("client_id"),
            "component_id": node.get("component_id"),
            "hardware": node.find(
                ".//{http://www.protogeni.net/resources/rspec/ext/emulab/1}vnode"
            ).get("hardware_type"),
            "os_image": node.find(
                ".//{http://www.protogeni.net/resources/rspec/ext/emulab/1}vnode"
            ).get("disk_image"),
        }

        # Get host information
        host = node.find(".//{http://www.geni.net/resources/rspec/3}host")
        if host is not None:
            node_info["hostname"] = host.get("name")
            node_info["public_ip"] = host.get("ipv4")

        # Get interface information
        interface = node.find(".//{http://www.geni.net/resources/rspec/3}interface")
        if interface is not None:
            ip = interface.find(".//{http://www.geni.net/resources/rspec/3}ip")
            if ip is not None:
                node_info["internal_ip"] = ip.get("address")
                node_info["netmask"] = ip.get("netmask")

        nodes.append(node_info)

    # Get location information
    location = root.find(
        ".//{http://www.protogeni.net/resources/rspec/ext/site-info/1}location"
    )
    location_info = {
        "country": location.get("country") if location is not None else None,
        "latitude": location.get("latitude") if location is not None else None,
        "longitude": location.get("longitude") if location is not None else None,
    }

    return {
        "description": description,
        "expiration": expiration,
        "nodes": nodes,
        "location": location_info,
    }


def collect_hardware_info_from_html():
    PORTAL_HARDWARE_URL = "https://www.cloudlab.us/portal-hardware.php"
    try:
        response = requests.get(PORTAL_HARDWARE_URL)
        response.raise_for_status()
        html_content = response.text

        soup = BeautifulSoup(html_content, "html.parser")

        amlist_script_tag = soup.find(
            "script", {"id": "amlist-json", "type": "text/plain"}
        )

        if not amlist_script_tag:
            print("Error: Could not find the 'amlist-json' script tag in the HTML.")
            return None

        escaped_json_string = amlist_script_tag.string
        if not escaped_json_string:
            print("Error: 'amlist-json' script tag is empty.")
            return None

        unescaped_json_string = html.unescape(escaped_json_string)

        amlist_data = json.loads(unescaped_json_string)

        extracted_hardware_list = []
        for urn_key, urn_info in amlist_data.items():
            if not isinstance(urn_info, dict):
                # print(f"Skipping URN {urn_key} as its info is not a dictionary.")
                continue

            cluster_name = urn_info.get("name", "N/A")
            typeinfo = urn_info.get("typeinfo")

            if cluster_name not in [
                "Cloudlab Clemson",
                "Cloudlab Utah",
                "Cloudlab Wisconsin",
            ]:
                continue

            if isinstance(typeinfo, dict):
                for hw_name, hw_stats in typeinfo.items():
                    if isinstance(hw_stats, dict):
                        total_count = hw_stats.get("count", 0)
                        free_count = hw_stats.get("free", 0)

                        extracted_hardware_list.append(
                            {
                                "hardware_name": hw_name,
                                "cluster_name": cluster_name,
                                "urn": urn_key,  # The URN of the cluster/AM
                                "total": total_count,
                                "free": free_count,
                            }
                        )
                    else:
                        print(
                            f"Warning: Expected dict for hardware stats of '{hw_name}' in URN '{urn_key}', got {type(hw_stats)}"
                        )
            elif isinstance(typeinfo, list) and not typeinfo:
                print(
                    f"Info: URN '{urn_key}' ('{cluster_name}') has empty list for typeinfo."
                )
            else:
                print(
                    f"Warning: typeinfo for URN '{urn_key}' ('{cluster_name}') is not a dict or empty list: {type(typeinfo)}"
                )

        return extracted_hardware_list

    except requests.exceptions.RequestException as e:
        print(f"Error fetching HTML: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"Error decoding amlist JSON from HTML: {e}")
        return None
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return None

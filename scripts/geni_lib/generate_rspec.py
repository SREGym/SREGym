import geni.portal as portal

RSPEC_FILE = "rspecs/test.xml"

# Create a Request object to start building the RSpec
request = portal.context.makeRequestRSpec()

# Create the raw "PC" nodes
node1 = request.RawPC("control")
node2 = request.RawPC("compute1")
node3 = request.RawPC("compute2")

# Set the hardware type
node1.hardware_type = "c220g5"
node2.hardware_type = "c220g5"
node3.hardware_type = "c220g5"

# Set the disk image
node1.disk_image = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD"
node2.disk_image = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD"
node3.disk_image = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD"

# node1.routable_control_ip = True
# node2.routable_control_ip = True
# node3.routable_control_ip = True

# Create a link between the two nodes
link1 = request.Link(members=[node1, node2, node3])

# Print the RSpec to the console
portal.context.printRequestRSpec()

# Save the RSpec to a file
with open(RSPEC_FILE, "w") as f:
    f.write(request.toXMLString(pretty_print=True, ucode=True))

## End-to-End Testing Automation 

The script will:
- Clone the SREGym repo on each node
- Install dependencies
- Create a kind cluster on each node
- Set up a Python virtual environment
- Run the benchmark in a tmux session on each node
- Run an automatic submission script on each node in another tmux session
- Collect logs from all nodes back to your local machine

### Prerequisites:

Before running the script, make sure:
1. **You have CloudLab nodes reserved** and can SSH into them
2.. **Your SSH key is added to your SSH agent** on your local machine to make sure you can access SREGym on the remote node

## Required Files:
1. Make sure you have a nodes.txt file in this directory, with each line being a cloudlab node hostname
    ex: 
    lilygn@c220g2-010810.wisc.cloudlab.us
    lilygn@c220g2-010812.wisc.cloudlab.us  
2. Make sure registry.txt is up to date with all of the problems you want to test

### How to use:

1. On your local machine, run the automating_tests.py script
2. You will be prompted for your CloudLab username


## Log files:

You will be able to see the output from each node in the node_logs directory.



from sregym.generators.noise.base import BaseNoise
from sregym.generators.noise.impl import register_noise
from sregym.service.kubectl import KubeCtl
import logging
import tempfile
import os
import yaml

logger = logging.getLogger(__name__)

@register_noise("zombie_resources")
class ZombieResourcesNoise(BaseNoise):
    def __init__(self, config):
        super().__init__(config)
        self.kubectl = KubeCtl()
        self.namespace = config.get("namespace", "default")
        self.schedule = config.get("schedule", "*/1 * * * *") # Every minute
        self.job_name = config.get("job_name", "system-cache-cleaner")
        self.created_resources = []
        self.context = {}

    def inject(self, context=None):
        # Only create once
        if self.created_resources:
            return
        
        # Use context namespace if available
        target_ns = self.context.get("namespace", self.namespace)

        logger.info(f"Creating Zombie Resource CronJob in {target_ns}")
        
        cronjob_manifest = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {
                "name": self.job_name,
                "namespace": target_ns
            },
            "spec": {
                "schedule": self.schedule,
                "successfulJobsHistoryLimit": 0,
                "failedJobsHistoryLimit": 5, # Keep 5 failed jobs
                "jobTemplate": {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{
                                    "name": "cleaner",
                                    "image": "busybox",
                                    "command": ["/bin/sh", "-c", "exit 1"] # Fail immediately
                                }],
                                "restartPolicy": "Never"
                            }
                        }
                    }
                }
            }
        }

        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as tmp:
                yaml.dump(cronjob_manifest, tmp)
                tmp_path = tmp.name
            
            self.kubectl.exec_command(f"kubectl apply -f {tmp_path}")
            os.remove(tmp_path)
            self.created_resources.append((self.job_name, target_ns))
            
        except Exception as e:
            logger.error(f"Failed to create zombie cronjob: {e}")

    def clean(self):
        if not self.created_resources:
            return
            
        logger.info("Cleaning up zombie resources")
        for job_name, ns in self.created_resources:
            try:
                # Delete the CronJob
                self.kubectl.exec_command(f"kubectl delete cronjob {job_name} -n {ns} --ignore-not-found")
                # Delete the Jobs created by it (cascade should handle it, but explicit is safer for cleanup)
                self.kubectl.exec_command(f"kubectl delete jobs -n {ns} -l job-name={job_name} --ignore-not-found")
            except Exception as e:
                logger.error(f"Failed to clean zombie resources {job_name}: {e}")
        self.created_resources = []

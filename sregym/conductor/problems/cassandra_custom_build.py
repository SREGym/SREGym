"""Base class for Cassandra problems that compile and deploy user-modified source code.

Usage
-----
1. Create a patch directory whose layout mirrors the Cassandra source tree.
   Only the files you want to modify need to be present::

       sregym/conductor/problems/patches/my_bug/
           src/java/org/apache/cassandra/db/SomeClass.java   ← modified

2. Subclass ``CassandraCustomBuildProblem`` and set the required class attributes::

       from pathlib import Path
       from sregym.conductor.problems.cassandra_custom_build import CassandraCustomBuildProblem

       class MyCassandraBug(CassandraCustomBuildProblem):
           cassandra_version   = "4.1.7"
           source_git_ref      = "cassandra-4.1.7"
           patch_dir           = Path(__file__).parent / "patches" / "my_bug"
           trigger_cql         = "SELECT * FROM ..."
           root_cause_file     = "src/java/org/apache/cassandra/db/SomeClass.java"
           root_cause_description = "..."

3. Register in ProblemRegistry.

Build pipeline
--------------
- Clone Cassandra at ``source_git_ref`` (cached by SourceManager).
- Overlay files from ``patch_dir`` onto the clone.
- ``ant jar`` → produces ``build/apache-cassandra-<version>.jar`` (incremental; cached by patch hash).
- ``docker build`` → image ``sregym/cassandra-patched:<version>-<hash8>``.
- ``kind load docker-image`` → available inside the kind cluster.
- K8ssandraCluster CR uses ``serverImage: <image>`` so the operator runs the patched binary.
- The same patched source tree is bind-mounted into the agent container at ``/opt/source``.

Prerequisites
-------------
- Apache Ant must be installed (``brew install ant`` / ``apt-get install ant``).
- Docker must be running.
- The cluster must be a kind cluster (for ``kind load docker-image``); for other
  cluster types push the image to an accessible registry instead.
"""

import logging
from pathlib import Path

from sregym.conductor.problems.cassandra_bug import CASSANDRA_REPO_URL, CassandraBugProblem
from sregym.service.apps.cassandra import Cassandra, CassandraWithCustomImage
from sregym.service.cassandra_build_manager import CassandraBuildManager
from sregym.service.source_manager import SourceManager

logger = logging.getLogger(__name__)


class CassandraCustomBuildProblem(CassandraBugProblem):
    """Deploy Cassandra with a custom-built image containing user-modified source files.

    Subclasses must set ``patch_dir`` in addition to the attributes required by
    ``CassandraBugProblem`` (``cassandra_version``, ``source_git_ref``,
    ``trigger_cql``, ``root_cause_file``, ``root_cause_description``).
    """

    # Path to a directory of modified .java files (mirrors Cassandra source tree).
    # Typically set as:  patch_dir = Path(__file__).parent / "patches" / "my_bug"
    patch_dir: Path

    def __init__(self):
        # Clone source first so it is available for both the build step and
        # the bind-mount into the agent container.  SourceManager is idempotent.
        source_manager = SourceManager()
        source_path = source_manager.ensure_source(
            repo_url=CASSANDRA_REPO_URL,
            git_ref=self.source_git_ref,
            name="cassandra",
        )

        # CRITICAL: Reset source to original git state before applying patches.
        # This ensures each benchmark run starts from the same baseline, even if
        # a previous agent modified the source (e.g., to fix a bug). Without this,
        # the next run would see an already-fixed source and the patch would have
        # no effect.
        source_manager.reset_source(source_path)

        # Build the custom image (skipped if already cached for this patch hash).
        build_mgr = CassandraBuildManager(source_path, self.cassandra_version)
        self._custom_image = build_mgr.build_with_patches(Path(self.patch_dir))
        logger.info(f"[CustomBuild] Using image: {self._custom_image}")

        # Normal init: source clone is a no-op (already done above), deploys
        # the app via _create_app(), sets up the diagnosis oracle, etc.
        super().__init__()

    def _create_app(self) -> Cassandra:
        """Return a Cassandra app configured to use the custom-built image."""
        return CassandraWithCustomImage(
            cassandra_version=self.cassandra_version,
            custom_image=self._custom_image,
        )

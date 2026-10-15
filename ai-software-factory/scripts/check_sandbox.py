"""Reports which sandbox isolation your Pod actually provides. Run it on the Pod, read the output honestly."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import load_settings  # noqa: E402
from app.execution.sandbox import Sandbox  # noqa: E402

s = load_settings({"PROVIDER": "fake", "AUTHORIZED_USER_IDS": "1"})
sb = Sandbox(backend=s.sandbox_backend, memory_mb=s.sandbox_memory_mb, timeout_s=s.sandbox_timeout_s,
             allow_network=s.sandbox_allow_network, docker_image=s.sandbox_docker_image)
caps = sb.capabilities()
print(json.dumps(caps, indent=2))
if not caps["network_isolated"] or not caps["filesystem_restricted"]:
    print("\nWARNING: generated code runs with weaker isolation than ideal on this Pod "
          "(RunPod Pods are containers; nested Docker/namespaces are often unavailable).\n"
          "Only submit projects you are comfortable running on this machine; secrets are scrubbed from the "
          "child environment but the filesystem is not fully hidden.")

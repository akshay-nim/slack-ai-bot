# backend/exceptions.py

class PodNotFoundError(Exception):
    """Raised when the requested Pod does not exist in the given namespace."""
    pass

class KubeconfigNotFoundError(Exception):
    """Raised when the kubeconfig file for a cluster is missing or unreadable."""
    pass

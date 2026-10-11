from gac_connect import GacClient, ResponseError
from gac_connect.client import GacClient as ModuleClient
from gac_connect.errors import GacError


def test_documented_client_is_available_from_package_and_module():
    assert GacClient is ModuleClient
    assert issubclass(ResponseError, GacError)

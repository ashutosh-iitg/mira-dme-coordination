from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig

from mira.config import ConfigError, temporal_profile


async def connect() -> Client:
    """Connect to Temporal Cloud using the CLI profile (temporal.toml), so the API key never lives in this repo."""
    profile = temporal_profile()
    connect_config = ClientConfig.load_client_connect_config(profile=profile)
    if not connect_config.get("api_key"):
        raise ConfigError(
            f"Temporal profile {profile!r} has no API key. Run the Temporal Cloud setup "
            "(temporal cloud login + apikey create-for-me) or set TEMPORAL_PROFILE."
        )
    return await Client.connect(**connect_config, data_converter=pydantic_data_converter)

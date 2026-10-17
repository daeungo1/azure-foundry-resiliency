"""Generate editable examples; every endpoint and subscription ID is a placeholder."""

from .config import TARGET_MODELS, Backend, Settings


def example_settings(*, mixed: bool, subscriptions: int = 1, include_eu: bool = False) -> Settings:
    if subscriptions not in {1, 2}:
        raise ValueError("Examples support one or two subscriptions")
    regions = ["koreacentral", "uksouth", "eastus2", "canadaeast"]
    if include_eu:
        regions.append("swedencentral")
    backends = []
    for subscription in range(subscriptions):
        label = chr(ord("a") + subscription)
        for model, version in TARGET_MODELS.items():
            for region in regions:
                account = f"replace-{label}-{region}"
                backends.append(
                    Backend(
                        id=f"{label}-{region}-{model}",
                        model=model,
                        version=version,
                        deployment=model,
                        region=region,
                        priority=subscription,
                        base_url=f"https://{account}.openai.azure.com/openai/v1/",
                        resource_id=(
                            "/subscriptions/00000000-0000-0000-0000-000000000000"
                            f"/resourceGroups/replace-rg/providers/Microsoft.CognitiveServices"
                            f"/accounts/{account}"
                        ),
                        # Conservative example for models onboarded to subscription-level quota.
                        quota_pool=f"subscription-{label}:{model}:{version}:global",
                    )
                )
    models = list(TARGET_MODELS)
    return Settings(
        backends=backends,
        max_attempts=8 if mixed else 4,
        fallbacks={
            model: [candidate for candidate in models if candidate != model] for model in models
        }
        if mixed
        else {},
        region_preferences={
            "KR": ["koreacentral", "eastus2", "canadaeast", "uksouth", "swedencentral"],
            "UK": ["uksouth", "swedencentral", "eastus2", "canadaeast", "koreacentral"],
            "US": ["eastus2", "canadaeast", "uksouth", "swedencentral", "koreacentral"],
            "CA": ["canadaeast", "eastus2", "uksouth", "swedencentral", "koreacentral"],
            "EU": ["swedencentral", "uksouth", "eastus2", "canadaeast", "koreacentral"],
        },
    )

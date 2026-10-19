"""Generate editable examples; every endpoint and subscription ID is a placeholder."""

from .config import TARGET_MODELS, Backend, Settings


def example_settings(
    *,
    mixed: bool,
    subscriptions: int = 1,
    include_expansion: bool = False,
    region_count: int | None = None,
) -> Settings:
    if subscriptions not in {1, 2}:
        raise ValueError("Examples support one or two subscriptions")
    if region_count is not None and include_expansion:
        raise ValueError("Choose either an explicit region count or include-expansion")
    count = region_count if region_count is not None else (5 if include_expansion else 4)
    if not 1 <= count <= 16:
        raise ValueError("Example region count must be between 1 and 16")
    regions = [f"Region{number:02d}" for number in range(1, count + 1)]
    backends = []
    for subscription in range(subscriptions):
        label = chr(ord("a") + subscription)
        for model, version in TARGET_MODELS.items():
            for region in regions:
                account = f"replace-{label}-{region.lower()}"
                backends.append(
                    Backend(
                        id=f"{label}-{region}-{model}",
                        model=model,
                        version=version,
                        deployment=model,
                        region=region,
                        subscription=f"Subscription{subscription + 1:02d}",
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
    preferences = {
        "Region01": ["Region01", "Region03", "Region04", "Region02", "Region05"],
        "Region02": ["Region02", "Region05", "Region03", "Region04", "Region01"],
        "Region03": ["Region03", "Region04", "Region02", "Region05", "Region01"],
        "Region04": ["Region04", "Region03", "Region02", "Region05", "Region01"],
        "Region05": ["Region05", "Region02", "Region03", "Region04", "Region01"],
    }
    if region_count is not None:
        preferences = {
            region: list(
                dict.fromkeys(
                    [
                        candidate
                        for candidate in preferences.get(region, [region])
                        if candidate in regions
                    ]
                    + regions
                )
            )
            for region in regions
        }
    return Settings(
        backends=backends,
        max_attempts=min(20, subscriptions * count * (len(models) if mixed else 1)),
        fallbacks={
            model: [candidate for candidate in models if candidate != model] for model in models
        }
        if mixed
        else {},
        region_preferences=preferences,
    )

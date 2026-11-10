"""Configured provider failover through the host's trusted plugin LLM boundary."""

import logging

logger = logging.getLogger(__name__)


def _provider_failure(error):
    status = getattr(error, 'status_code', None)
    return (isinstance(status, int) and (status in {401, 402, 403, 404, 408, 424, 429} or 500 <= status < 600)
            or isinstance(error, (ConnectionError, TimeoutError))
            or type(error).__name__ in {'APIConnectionError', 'APITimeoutError'})


def complete_structured(llm, **kwargs):
    try:
        return llm.complete_structured(**kwargs)
    except Exception as error:
        if not _provider_failure(error):
            raise
        last_error = error
    from hermes_cli.config import load_config_readonly
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.providers import normalize_provider

    def identity(provider, model):
        return normalize_provider(str(provider or '')), str(model or '').lower()

    config = load_config_readonly()
    main = config.get('model') or {}
    main = main if isinstance(main, dict) else {'default': main}
    seen = {identity(kwargs.get('provider') or main.get('provider'), kwargs.get('model') or main.get('default'))}
    for entry in get_fallback_chain(config):
        if any(entry.get(key) for key in ('base_url', 'api_key', 'api_key_env', 'key_env', 'api_mode')):
            logger.warning('Reviewer fallback skipped: inline transport/credential overrides require a named provider')
            continue
        provider, model = entry['provider'], entry['model']
        target = identity(provider, model)
        if target in seen:
            continue
        seen.add(target)
        logger.info('Reviewer provider failover to %s/%s', provider, model)
        try:
            return llm.complete_structured(**{**kwargs, 'provider': provider, 'model': model})
        except Exception as error:
            if not _provider_failure(error):
                raise
            last_error = error
    raise last_error

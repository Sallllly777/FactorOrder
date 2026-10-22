"""Copy this file into your trusted data-ingestion code, then fill the callbacks.

No SDK is imported and no connection is made until YOUR callback is called.
Start with bars only; add the other callbacks only when you have those datasets.
"""
from pkuquant import FunctionProvider,from_long,SourceProvider,QMTSource


def make_provider(client):
    def bars(request):
        # Replace fetch_daily with your vendor's actual API and date semantics.
        raw=client.fetch_daily(symbols=request.symbols,start=request.start,end=request.end)
        return from_long(raw,amount_unit='CNY',volume_unit='shares',provider='your-vendor')

    # Missing functions are NOT registered as fake capabilities.
    return FunctionProvider(name='your-vendor',version='1',bars=bars)


def make_qmt_provider():
    return SourceProvider(
        lambda request:QMTSource(request.symbols,request.start,request.end,volume_unit='lots100'),
        name='your-broker-qmt',version='1',
    )


# When real history is available, register additional callbacks like this:
# FunctionProvider(name='vendor',version='2',bars=bars,
#     calendar=lambda request: client.calendar(request.start,request.end),
#     security_history=lambda request: client.st_events_with_known_at(request.symbols,request.end),
#     trading_states=lambda request: client.daily_rules(request.symbols,request.start,request.end))
# Normalize these outputs to the schemas in docs/provider_contract.md first.
# Never derive historical known_at from today's instrument name or download time.

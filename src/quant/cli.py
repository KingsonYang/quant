import typer

from quant.commands import calendar, discover, evolve, experiment, ingest, legacy, optimize, run, strategy, summarize, validate, web

app = typer.Typer(help="Utilities for the quant project.")
app.add_typer(strategy.app, name="strategy")
app.add_typer(run.app, name="run")
app.add_typer(experiment.app, name="experiment")
app.add_typer(summarize.app, name="summarize")

calendar.register(app)
ingest.register(app)
legacy.register(app)
optimize.register(app)
validate.register(app)
evolve.register(app)
discover.register(app)
web.register(app)


if __name__ == "__main__":
    app()

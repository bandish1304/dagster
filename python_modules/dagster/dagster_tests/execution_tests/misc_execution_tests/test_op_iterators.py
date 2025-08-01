import dagster as dg
from dagster._annotations import beta
from dagster._utils.test import wrap_op_in_graph_and_execute


def test_generator_return_op():
    def _gen():
        yield dg.Output("done")

    @dg.op
    def gen_ret_op(_):
        return _gen()

    result = wrap_op_in_graph_and_execute(gen_ret_op)
    assert result.output_value() == "done"


def test_generator_yield_op():
    def _gen():
        yield dg.Output("done")

    @dg.op
    def gen_yield_op(_):
        yield from _gen()

    result = wrap_op_in_graph_and_execute(gen_yield_op)
    assert result.output_value() == "done"


def test_generator_yield_from_op():
    def _gen():
        yield dg.Output("done")

    @dg.op
    def gen_yield_op(_):
        yield from _gen()

    result = wrap_op_in_graph_and_execute(gen_yield_op)
    assert result.output_value() == "done"


def test_nested_generator_op():
    def _gen1():
        yield dg.AssetMaterialization("test")

    def _gen2():
        yield dg.Output("done")

    def _gen():
        yield from _gen1()
        yield from _gen2()

    @dg.op
    def gen_return_op(_):
        return _gen()

    result = wrap_op_in_graph_and_execute(gen_return_op)
    assert result.output_value() == "done"


def test_beta_generator_op():
    @dg.op
    @beta
    def gen_op():
        yield dg.Output("done")

    result = wrap_op_in_graph_and_execute(gen_op)
    assert result.output_value() == "done"

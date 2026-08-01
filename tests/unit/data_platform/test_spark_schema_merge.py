from features.spark.session import _env_or_default, read_parquet_table


class _Reader:
    def __init__(self):
        self.options = {}
        self.path = None

    def option(self, name, value):
        self.options[name] = value
        return self

    def parquet(self, path):
        self.path = path
        return _Frame()


class _Frame:
    columns = []


class _Spark:
    def __init__(self):
        self.read = _Reader()


def test_read_parquet_table_enables_schema_merge_per_read():
    spark = _Spark()
    read_parquet_table(spark, "s3a://lake/warehouse", "behavior_events")
    assert spark.read.options == {"mergeSchema": "true"}
    assert spark.read.path == "s3a://lake/warehouse/behavior_events"


def test_empty_spark_size_environment_uses_safe_default(monkeypatch):
    monkeypatch.setenv("SPARK_ADVISORY_PARTITION_SIZE_BYTES", "")
    assert _env_or_default("SPARK_ADVISORY_PARTITION_SIZE_BYTES", "134217728") == "134217728"

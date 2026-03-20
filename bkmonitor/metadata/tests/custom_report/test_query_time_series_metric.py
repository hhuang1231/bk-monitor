"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2025 Tencent. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

import pytest

from metadata import models
from metadata.resources.resources import QueryTimeSeriesMetricResource

pytestmark = pytest.mark.django_db(databases="__all__")

DEFAULT_GROUP_ID = 100
DEFAULT_TABLE_ID = "test_mandatory.__default__"


@pytest.fixture
def setup_metrics():
    """创建测试用的 TimeSeriesGroup 和 TimeSeriesMetric 数据"""
    models.TimeSeriesGroup.objects.create(
        bk_data_id=100,
        bk_biz_id=1,
        table_id=DEFAULT_TABLE_ID,
        is_split_measurement=True,
        time_series_group_id=DEFAULT_GROUP_ID,
    )

    # 创建 3 个 scope
    scope1 = models.TimeSeriesScope.objects.create(
        group_id=DEFAULT_GROUP_ID,
        scope_name="scope_a",
    )
    scope2 = models.TimeSeriesScope.objects.create(
        group_id=DEFAULT_GROUP_ID,
        scope_name="scope_b",
    )

    # 创建指标，覆盖不同 scope 和 field_config（disabled）
    models.TimeSeriesMetric.objects.bulk_create(
        [
            # metric1: scope_a, not disabled, name=cpu_usage
            models.TimeSeriesMetric(
                group_id=DEFAULT_GROUP_ID,
                table_id=f"{DEFAULT_TABLE_ID}.cpu_usage",
                field_id=1001,
                field_name="cpu_usage",
                scope_id=scope1.id,
                tag_list=["host"],
                field_config={"disabled": False, "alias": "CPU Usage"},
            ),
            # metric2: scope_a, disabled=True, name=mem_usage
            models.TimeSeriesMetric(
                group_id=DEFAULT_GROUP_ID,
                table_id=f"{DEFAULT_TABLE_ID}.mem_usage",
                field_id=1002,
                field_name="mem_usage",
                scope_id=scope1.id,
                tag_list=["host"],
                field_config={"disabled": True, "alias": "Memory Usage"},
            ),
            # metric3: scope_b, not disabled, name=disk_usage
            models.TimeSeriesMetric(
                group_id=DEFAULT_GROUP_ID,
                table_id=f"{DEFAULT_TABLE_ID}.disk_usage",
                field_id=1003,
                field_name="disk_usage",
                scope_id=scope2.id,
                tag_list=["disk_name"],
                field_config={"disabled": False, "alias": "Disk Usage"},
            ),
            # metric4: scope_b, not disabled, name=net_usage
            models.TimeSeriesMetric(
                group_id=DEFAULT_GROUP_ID,
                table_id=f"{DEFAULT_TABLE_ID}.net_usage",
                field_id=1004,
                field_name="net_usage",
                scope_id=scope2.id,
                tag_list=["interface"],
                field_config={"disabled": False, "alias": "Network Usage"},
            ),
        ]
    )
    yield {
        "scope1_id": scope1.id,
        "scope2_id": scope2.id,
    }
    models.TimeSeriesMetric.objects.filter(group_id=DEFAULT_GROUP_ID).delete()
    models.TimeSeriesScope.objects.filter(group_id=DEFAULT_GROUP_ID).delete()
    models.TimeSeriesGroup.objects.filter(time_series_group_id=DEFAULT_GROUP_ID).delete()


class TestApplySearchFilters:
    """测试 QueryTimeSeriesMetricResource._apply_search_filters 方法"""

    def _get_resource(self):
        return QueryTimeSeriesMetricResource()

    def _get_base_query_set(self):
        return models.TimeSeriesMetric.objects.filter(group_id=DEFAULT_GROUP_ID)

    def _get_field_names(self, query_set):
        return sorted(query_set.values_list("field_name", flat=True))

    def test_no_conditions_no_mandatory(self, setup_metrics):
        """两者都没有时，返回所有结果"""
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        result = resource._apply_search_filters(query_set, {})
        assert result.count() == 4

    def test_only_conditions_with_and(self, setup_metrics):
        """只有 conditions（AND 连接）"""
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        validated_data = {
            "conditions": [
                {"key": "name", "values": ["cpu"], "search_type": "fuzzy"},
            ],
            "condition_connector": "and",
        }
        result = resource._apply_search_filters(query_set, validated_data)
        assert self._get_field_names(result) == ["cpu_usage"]

    def test_only_mandatory_conditions(self, setup_metrics):
        """只有 mandatory_conditions"""
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        validated_data = {
            "mandatory_conditions": [
                {"key": "field_config_disabled", "values": ["false"], "search_type": "exact"},
            ],
        }
        result = resource._apply_search_filters(query_set, validated_data)
        # mem_usage is disabled, should be excluded
        assert result.count() == 3
        assert "mem_usage" not in self._get_field_names(result)

    def test_or_connector_without_mandatory(self, setup_metrics):
        """condition_connector=or 时，所有 conditions 都被 OR 连接"""
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        validated_data = {
            "conditions": [
                {"key": "name", "values": ["cpu_usage"], "search_type": "exact"},
                {"key": "name", "values": ["disk_usage"], "search_type": "exact"},
            ],
            "condition_connector": "or",
        }
        result = resource._apply_search_filters(query_set, validated_data)
        assert self._get_field_names(result) == ["cpu_usage", "disk_usage"]

    def test_or_connector_with_mandatory_disabled_filter(self, setup_metrics):
        """关键场景：condition_connector=or + mandatory_conditions

        期望语义：mandatory AND (user_cond1 OR user_cond2)
        即 disabled=false AND (name=cpu_usage OR name=disk_usage)
        """
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        validated_data = {
            "conditions": [
                {"key": "name", "values": ["cpu_usage"], "search_type": "exact"},
                {"key": "name", "values": ["disk_usage"], "search_type": "exact"},
            ],
            "mandatory_conditions": [
                {"key": "field_config_disabled", "values": ["false"], "search_type": "exact"},
            ],
            "condition_connector": "or",
        }
        result = resource._apply_search_filters(query_set, validated_data)
        field_names = self._get_field_names(result)
        assert field_names == ["cpu_usage", "disk_usage"]
        # mem_usage is disabled, so it must NOT appear even though connector is OR
        assert "mem_usage" not in field_names

    def test_or_connector_with_mandatory_scope_filter(self, setup_metrics):
        """关键场景：condition_connector=or + mandatory scope_id 过滤

        模拟 APM 场景：mandatory scope_id 过滤 + 用户 OR 搜索
        期望：scope_id=scope1 AND (name~cpu OR name~mem)
        """
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        scope1_id = setup_metrics["scope1_id"]

        validated_data = {
            "conditions": [
                {"key": "name", "values": ["cpu"], "search_type": "fuzzy"},
                {"key": "name", "values": ["mem"], "search_type": "fuzzy"},
            ],
            "mandatory_conditions": [
                {"key": "scope_id", "values": [str(scope1_id)], "search_type": "exact"},
            ],
            "condition_connector": "or",
        }
        result = resource._apply_search_filters(query_set, validated_data)
        field_names = self._get_field_names(result)
        # 只有 scope_a 下的 cpu_usage 和 mem_usage
        assert field_names == ["cpu_usage", "mem_usage"]
        # scope_b 下的指标不应出现
        assert "disk_usage" not in field_names
        assert "net_usage" not in field_names

    def test_or_connector_with_multiple_mandatory_conditions(self, setup_metrics):
        """多个 mandatory 条件始终以 AND 方式组合

        mandatory: scope_id=scope1 AND disabled=false
        user: name~cpu OR name~mem (or connector)
        期望：scope1 下非 disabled 且名称包含 cpu 或 mem 的指标
        """
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        scope1_id = setup_metrics["scope1_id"]

        validated_data = {
            "conditions": [
                {"key": "name", "values": ["cpu"], "search_type": "fuzzy"},
                {"key": "name", "values": ["mem"], "search_type": "fuzzy"},
            ],
            "mandatory_conditions": [
                {"key": "scope_id", "values": [str(scope1_id)], "search_type": "exact"},
                {"key": "field_config_disabled", "values": ["false"], "search_type": "exact"},
            ],
            "condition_connector": "or",
        }
        result = resource._apply_search_filters(query_set, validated_data)
        field_names = self._get_field_names(result)
        # scope1 下 cpu_usage(not disabled) 匹配，mem_usage(disabled) 不匹配 mandatory
        assert field_names == ["cpu_usage"]

    def test_and_connector_with_mandatory_behavior_unchanged(self, setup_metrics):
        """condition_connector=and 时，mandatory + conditions 都是 AND，行为应与之前一致"""
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        scope1_id = setup_metrics["scope1_id"]

        validated_data = {
            "conditions": [
                {"key": "name", "values": ["cpu"], "search_type": "fuzzy"},
            ],
            "mandatory_conditions": [
                {"key": "scope_id", "values": [str(scope1_id)], "search_type": "exact"},
                {"key": "field_config_disabled", "values": ["false"], "search_type": "exact"},
            ],
            "condition_connector": "and",
        }
        result = resource._apply_search_filters(query_set, validated_data)
        field_names = self._get_field_names(result)
        assert field_names == ["cpu_usage"]

    def test_empty_conditions_with_mandatory(self, setup_metrics):
        """conditions 为空列表时，只有 mandatory 生效"""
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        scope1_id = setup_metrics["scope1_id"]

        validated_data = {
            "conditions": [],
            "mandatory_conditions": [
                {"key": "scope_id", "values": [str(scope1_id)], "search_type": "exact"},
            ],
        }
        result = resource._apply_search_filters(query_set, validated_data)
        field_names = self._get_field_names(result)
        # 只返回 scope1 下的所有指标（cpu_usage, mem_usage）
        assert field_names == ["cpu_usage", "mem_usage"]

    def test_empty_mandatory_with_conditions(self, setup_metrics):
        """mandatory_conditions 为空列表时，只有 conditions 生效"""
        resource = self._get_resource()
        query_set = self._get_base_query_set()

        validated_data = {
            "conditions": [
                {"key": "name", "values": ["cpu"], "search_type": "fuzzy"},
            ],
            "mandatory_conditions": [],
            "condition_connector": "or",
        }
        result = resource._apply_search_filters(query_set, validated_data)
        assert self._get_field_names(result) == ["cpu_usage"]

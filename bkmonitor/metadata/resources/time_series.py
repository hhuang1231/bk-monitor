"""
Tencent is pleased to support the open source community by making 蓝鲸智云 - 监控平台 (BlueKing - Monitor) available.
Copyright (C) 2017-2025 Tencent. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""

from django.utils.translation import gettext as _
from rest_framework import serializers

from bkmonitor.utils.serializers import TenantIdField
from metadata import models


class BaseTimeSeriesScopeRequestSerializer(serializers.Serializer):
    """时序分组操作的基础请求序列化器，定义通用字段和模板方法"""

    bk_tenant_id = TenantIdField(label="租户ID")
    group_id = serializers.IntegerField(required=True, label="自定义时序数据源ID")

    def to_internal_value(self, data):
        # 步骤1: 基础字段验证
        validated_data = super().to_internal_value(data)

        # 步骤2: 验证并缓存时序分组对象
        validated_data["validated_time_series_group"] = self._validate_and_return_time_series_group(
            validated_data["bk_tenant_id"], validated_data["group_id"]
        )

        # 步骤3: 提取和预处理业务数据（由子类实现）
        self._extract_and_preprocess_data(validated_data)

        # 步骤4: 查询并缓存相关对象（由子类实现）
        self._query_and_cache_related_objects(validated_data)

        # 步骤5: 执行业务验证（由子类实现）
        self._perform_business_validation(validated_data)

        return validated_data

    @staticmethod
    def _validate_and_return_time_series_group(bk_tenant_id: str, group_id: int):
        try:
            return models.TimeSeriesGroup.objects.get(
                time_series_group_id=group_id, bk_tenant_id=bk_tenant_id, is_delete=False
            )
        except models.TimeSeriesGroup.DoesNotExist:
            raise ValueError(_(f"自定义时序分组不存在，请确认后重试: group_id={group_id}"))

    def _extract_and_preprocess_data(self, validated_data: dict) -> None:
        """提取和预处理业务数据（由子类实现）"""
        pass

    def _query_and_cache_related_objects(self, validated_data: dict) -> None:
        """查询并缓存相关对象（由子类实现）"""
        pass

    def _perform_business_validation(self, validated_data: dict) -> None:
        pass


class CreateOrUpdateTimeSeriesScopeRequestSerializer(BaseTimeSeriesScopeRequestSerializer):
    class ScopeSerializer(serializers.Serializer):
        scope_id = serializers.IntegerField(required=False, label="指标分组ID")
        scope_name = serializers.CharField(required=False, label="指标分组名", max_length=255)
        dimension_config = serializers.DictField(required=False, allow_null=True, label="分组下的维度配置")
        auto_rules = serializers.ListField(required=False, label="自动分组的匹配规则列表")

    scopes = serializers.ListField(
        required=True, child=ScopeSerializer(), label="批量创建或更新的分组列表", min_length=1
    )

    def _extract_and_preprocess_data(self, validated_data: dict) -> None:
        """提取和预处理业务数据：分离创建和更新的分组"""
        scopes_data = validated_data["scopes"]

        # 分离创建和更新的分组
        new_scopes_to_create = [s for s in scopes_data if not s.get("scope_id")]
        new_scopes_to_update = [s for s in scopes_data if s.get("scope_id")]

        validated_data["new_scopes_to_create"] = new_scopes_to_create
        validated_data["new_scopes_to_update"] = new_scopes_to_update

    def _query_and_cache_related_objects(self, validated_data: dict) -> None:
        """查询并缓存相关对象：获取要更新的分组对象和已存在的分组名"""
        # 批量获取要更新的分组对象
        scope_ids = [scope_data["scope_id"] for scope_data in validated_data["new_scopes_to_update"]]
        validated_data["old_scopes_to_update"] = list(models.TimeSeriesScope.objects.filter(id__in=scope_ids))

        # 获取该 group 下所有已存在的 scope_name
        validated_data["all_existing_scope_names"] = set(
            models.TimeSeriesScope.objects.filter(group_id=validated_data["group_id"]).values_list(
                "scope_name", flat=True
            )
        )

    def _perform_business_validation(self, validated_data: dict) -> None:
        """执行业务验证：验证创建和更新场景"""
        group_id = validated_data["group_id"]
        new_scopes_to_create = validated_data["new_scopes_to_create"]
        new_scopes_to_update = validated_data["new_scopes_to_update"]
        old_scopes_to_update = validated_data["old_scopes_to_update"]
        all_existing_scope_names = validated_data["all_existing_scope_names"]

        # 验证创建场景
        if new_scopes_to_create:
            self._validate_scopes_for_create(group_id, new_scopes_to_create, all_existing_scope_names)

        # 验证更新场景
        if new_scopes_to_update:
            self._validate_scopes_for_update(
                group_id, new_scopes_to_update, old_scopes_to_update, all_existing_scope_names
            )

    @staticmethod
    def _check_duplicate_names_in_batch(scope_names: list[str], context: str) -> None:
        """检查批次内部是否有重复的 scope_name"""
        name_indices = {}
        for idx, name in enumerate(scope_names):
            name_indices.setdefault(name, []).append(idx)

        duplicates = {name: indices for name, indices in name_indices.items() if len(indices) > 1}
        if duplicates:
            error_msg = "; ".join(
                [f"scope_name={name}, 位置索引={', '.join(map(str, indices))}" for name, indices in duplicates.items()]
            )
            raise ValueError(_(f"批次内存在重复的分组名({context}): {error_msg}"))

    def _validate_scopes_for_create(
        self, group_id: int, new_scopes_to_create: list[dict], all_existing_scope_names: set
    ) -> None:
        """验证批量创建的分组数据"""
        # 1. 验证必填项并收集 scope_name
        create_scope_names = []
        for scope_data in new_scopes_to_create:
            scope_name = scope_data.get("scope_name")
            if not scope_name:
                raise ValueError(_("创建指标分组时，scope_name 为必填项"))
            create_scope_names.append(scope_name)

        # 2. 检查是否与数据库中已存在的 scope_name 冲突
        duplicate_scopes = [f"{group_id}:{name}" for name in create_scope_names if name in all_existing_scope_names]
        if duplicate_scopes:
            raise ValueError(_(f"指标分组名已存在，请确认后重试: {', '.join(duplicate_scopes)}"))

        # 3. 检查批次内部是否有重复的 scope_name
        self._check_duplicate_names_in_batch(create_scope_names, f"group_id={group_id}")

    def _validate_scopes_for_update(
        self,
        group_id: int,
        new_scopes_to_update: list[dict],
        old_scopes_to_update: list,
        all_existing_scope_names: set,
    ) -> None:
        """验证批量更新的分组数据"""
        # 1. 检查 scope 是否存在
        scope_ids_requested = {scope_data["scope_id"] for scope_data in new_scopes_to_update}
        scope_ids_found = {scope.id for scope in old_scopes_to_update}
        missing_scope_ids = scope_ids_requested - scope_ids_found
        if missing_scope_ids:
            raise ValueError(
                _("指标分组不存在，请确认后重试: scope_id={}").format(", ".join(map(str, missing_scope_ids)))
            )

        # 2. 构建 scope_id 到对象的映射，方便后续查找
        validated_scopes_map = {scope.id: scope for scope in old_scopes_to_update}

        # 3. 验证 group_id 匹配
        mismatched = [(scope.id, scope.group_id) for scope in old_scopes_to_update if scope.group_id != group_id]
        if mismatched:
            scope_id, actual_group_id = mismatched[0]
            raise ValueError(
                _("指标分组的 group_id 不匹配: scope_id={}, 期望 group_id={}, 实际 group_id={}").format(
                    scope_id, group_id, actual_group_id
                )
            )

        # 4. 收集更新后的分组名并检查批次内重复
        final_scope_names = []
        for scope_data in new_scopes_to_update:
            scope_obj = validated_scopes_map[scope_data["scope_id"]]
            final_name = scope_data.get("scope_name") or scope_obj.scope_name
            final_scope_names.append(final_name)

        self._check_duplicate_names_in_batch(final_scope_names, "更新场景")

        # 5. 检查分组名修改的合法性
        for scope_data in new_scopes_to_update:
            new_scope_name = scope_data.get("scope_name")
            if not new_scope_name:
                continue

            scope_obj = validated_scopes_map[scope_data["scope_id"]]
            # 如果名称没有变化，跳过
            if new_scope_name == scope_obj.scope_name:
                continue

            # 检查是否是数据自动创建的分组
            if scope_obj.is_create_from_data_or_default():
                raise ValueError(
                    _("数据自动创建的分组不允许修改分组名: scope_id={}, scope_name={}").format(
                        scope_obj.id, scope_obj.scope_name
                    )
                )

            # 检查新名称是否与其他已存在的分组冲突
            if new_scope_name in all_existing_scope_names:
                raise ValueError(_("指标分组名已存在: scope_name={}").format(new_scope_name))


class DeleteTimeSeriesScopeRequestSerializer(BaseTimeSeriesScopeRequestSerializer):
    class ScopeSerializer(serializers.Serializer):
        scope_name = serializers.CharField(required=True, label="指标分组名", max_length=255)

    scopes = serializers.ListField(required=True, child=ScopeSerializer(), label="批量删除的分组列表", min_length=1)

    def _extract_and_preprocess_data(self, validated_data: dict) -> None:
        """提取和预处理业务数据：提取要删除的分组名称集合"""
        validated_data["scope_names_to_delete"] = {
            scope_data.get("scope_name") for scope_data in validated_data.get("scopes", [])
        }

    def _query_and_cache_related_objects(self, validated_data: dict) -> None:
        """查询并缓存相关对象：批量获取要删除的分组对象"""
        validated_data["old_scopes_to_delete"] = list(
            models.TimeSeriesScope.objects.filter(
                group_id=validated_data["group_id"], scope_name__in=validated_data["scope_names_to_delete"]
            )
        )

    def _perform_business_validation(self, validated_data: dict) -> None:
        """执行业务验证：检查分组是否存在、是否允许删除"""
        scope_names_to_delete = validated_data["scope_names_to_delete"]
        old_scopes_to_delete = validated_data["old_scopes_to_delete"]

        # 检查是否所有 scope 都存在
        found_scope_names = {s.scope_name for s in old_scopes_to_delete}
        missing_scope_names = scope_names_to_delete - found_scope_names
        if missing_scope_names:
            raise ValueError(_("指标分组不存在，请确认后重试: {}").format(", ".join(missing_scope_names)))

        # 检查是否有数据自动创建的分组，不允许删除
        auto_created_scope_names = [s.scope_name for s in old_scopes_to_delete if s.is_create_from_data_or_default()]
        if auto_created_scope_names:
            raise ValueError(_(f"不允许删除数据自动创建的分组: {', '.join(auto_created_scope_names)}"))


class QueryTimeSeriesScopeRequestSerializer(BaseTimeSeriesScopeRequestSerializer):
    """查询自定义时序指标分组列表的请求序列化器"""

    scope_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, label="指标分组ID列表", allow_empty=True
    )
    scope_name = serializers.CharField(required=False, label="指标分组名称")
    include_metrics = serializers.BooleanField(required=False, default=False, label="是否返回指标数据")

    def _query_and_cache_related_objects(self, validated_data: dict) -> None:
        """查询并缓存相关对象：根据查询条件构建 scope 查询集"""
        scope_ids = validated_data.get("scope_ids")
        scope_name = validated_data.get("scope_name")

        # 构建查询集
        query_set = models.TimeSeriesScope.objects.filter(group_id=validated_data["group_id"])

        if scope_ids:
            query_set = query_set.filter(id__in=scope_ids)

        if scope_name:
            query_set = query_set.filter(scope_name__icontains=scope_name)

        # 缓存查询集到 validated_data 中
        validated_data["scope_query_set"] = query_set


class CreateOrUpdateTimeSeriesMetricRequestSerializer(BaseTimeSeriesScopeRequestSerializer):
    """批量创建或更新自定义时序指标的请求序列化器"""

    class MetricSerializer(serializers.Serializer):
        """单个指标的序列化器"""

        field_id = serializers.IntegerField(required=False, label="字段ID")
        field_name = serializers.CharField(required=False, label="指标字段名称", max_length=255)
        field_scope = serializers.CharField(required=False, label="指标数据分组", max_length=255)
        tag_list = serializers.ListField(
            required=False, label="Tag列表", child=serializers.CharField(), allow_null=True
        )
        field_config = serializers.DictField(required=False, label="字段其他配置", allow_null=True)
        label = serializers.CharField(required=False, label="指标监控对象", max_length=255, allow_null=True)
        scope_id = serializers.IntegerField(required=True, label="指标分组ID")

    metrics = serializers.ListField(
        required=True,
        label="批量指标列表",
        child=MetricSerializer(),
        allow_empty=False,
    )

    def _extract_and_preprocess_data(self, validated_data: dict) -> None:
        """提取和预处理业务数据：分离创建和更新的指标"""
        metrics_data = validated_data["metrics"]

        # 分离创建和更新的指标
        new_metrics_to_create = [m for m in metrics_data if not m.get("field_id")]
        new_metrics_to_update = [m for m in metrics_data if m.get("field_id")]

        validated_data["new_metrics_to_create"] = new_metrics_to_create
        validated_data["new_metrics_to_update"] = new_metrics_to_update

    def _query_and_cache_related_objects(self, validated_data: dict) -> None:
        """查询并缓存相关对象：获取要更新的指标对象、已存在的禁用指标、scope对象，并处理禁用指标重启"""
        group_id = validated_data["group_id"]
        new_metrics_to_create = validated_data["new_metrics_to_create"]
        new_metrics_to_update = validated_data["new_metrics_to_update"]

        # 批量获取要更新的指标对象
        old_metrics_to_update = list(
            models.TimeSeriesMetric.objects.filter(
                field_id__in=[m["field_id"] for m in new_metrics_to_update], group_id=group_id
            )
        )

        # 批量获取已存在的禁用指标并构建映射
        existing_disabled_metrics_map = {
            (m.field_name, m.field_scope): m
            for m in models.TimeSeriesMetric.objects.filter(
                group_id=group_id, scope_id=models.TimeSeriesMetric.DISABLE_SCOPE_ID
            )
        }

        # 处理禁用指标重启：将需要重启的禁用指标从创建列表移到更新列表
        final_metrics_to_create = []
        for metric_data in new_metrics_to_create:
            field_name = metric_data.get("field_name")
            field_scope = metric_data.get("field_scope", models.TimeSeriesMetric.DEFAULT_DATA_SCOPE_NAME)
            disabled_metric = existing_disabled_metrics_map.get((field_name, field_scope))

            if disabled_metric:
                # 将禁用指标转换为更新操作
                metric_data_copy = {**metric_data, "field_id": disabled_metric.field_id}
                field_config = metric_data_copy.get("field_config") or {}
                metric_data_copy["field_config"] = {**field_config, "disabled": False}
                new_metrics_to_update.append(metric_data_copy)
                old_metrics_to_update.append(disabled_metric)
            else:
                final_metrics_to_create.append(metric_data)

        # 更新 validated_data 中的数据
        validated_data.update(
            {
                "new_metrics_to_create": final_metrics_to_create,
                "new_metrics_to_update": new_metrics_to_update,
                "old_metrics_to_update": old_metrics_to_update,
            }
        )

        # 批量获取所有 scope 对象并构建映射
        scopes_dict = {scope.id: scope for scope in models.TimeSeriesScope.objects.filter(group_id=group_id)}
        scopes_dict[models.TimeSeriesMetric.DISABLE_SCOPE_ID] = None
        validated_data["scopes_dict"] = scopes_dict

    def _perform_business_validation(self, validated_data: dict) -> None:
        """执行业务验证：验证创建和更新场景"""
        new_metrics_to_create = validated_data["new_metrics_to_create"]
        new_metrics_to_update = validated_data["new_metrics_to_update"]
        old_metrics_to_update = validated_data["old_metrics_to_update"]
        scopes_dict = validated_data["scopes_dict"]

        # 验证创建场景
        if new_metrics_to_create:
            self._validate_metrics_for_create(new_metrics_to_create, scopes_dict)

        # 验证更新场景
        if new_metrics_to_update:
            self._validate_metrics_for_update(new_metrics_to_update, old_metrics_to_update, scopes_dict)

    @staticmethod
    def _validate_metrics_for_create(new_metrics_to_create: list[dict], scopes_dict: dict) -> None:
        """验证批量创建的指标数据"""
        field_names = []
        for metric_data in new_metrics_to_create:
            # 验证必填项
            field_name = metric_data.get("field_name")
            if not field_name:
                raise ValueError(_("创建指标时，field_name为必填项"))
            field_names.append(field_name)

            # 验证 scope_id 是否存在
            scope_id = metric_data.get("scope_id")
            if scope_id not in scopes_dict:
                raise ValueError(_(f"指标分组不存在，请确认后重试。分组ID: {scope_id}"))

        # 检查批次内部是否有重复的 field_name
        seen = set()
        duplicates = [name for name in field_names if name in seen or seen.add(name)]
        if duplicates:
            raise ValueError(_(f"同一批次内指标字段名称[{', '.join(set(duplicates))}]重复，请使用其他名称"))

    @staticmethod
    def _validate_metrics_for_update(
        new_metrics_to_update: list[dict], old_metrics_to_update: list, scopes_dict: dict
    ) -> None:
        """验证批量更新的指标数据"""
        # 检查指标是否存在
        field_ids_requested = {m["field_id"] for m in new_metrics_to_update}
        field_ids_found = {metric.field_id for metric in old_metrics_to_update}
        if missing_ids := field_ids_requested - field_ids_found:
            raise ValueError(_("指标不存在，请确认后重试: field_id={}").format(", ".join(map(str, missing_ids))))

        # 构建 field_id 到对象的映射
        validated_metrics_map = {metric.field_id: metric for metric in old_metrics_to_update}

        # 验证 scope_id 和修改权限
        for metric_data in new_metrics_to_update:
            scope_id = metric_data.get("scope_id")
            if scope_id and scope_id not in scopes_dict:
                raise ValueError(_(f"指标分组不存在，请确认后重试。分组ID: {scope_id}"))

            # 验证是否允许修改 scope_id（数据自动创建的分组不允许修改）
            metric_obj = validated_metrics_map[metric_data["field_id"]]
            if scope_id and scope_id != metric_obj.scope_id:
                source_scope = scopes_dict.get(metric_obj.scope_id)
                if source_scope and source_scope.create_from == models.TimeSeriesScope.CREATE_FROM_DATA:
                    raise ValueError(
                        _(f"数据自动创建的指标分组不允许修改，请确认后重试。分组ID: {metric_obj.scope_id}")
                    )

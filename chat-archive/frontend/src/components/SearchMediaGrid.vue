<script setup lang="ts">
import { computed } from "vue";

import type { SearchHit } from "@/api/archiveApi";
import SearchHitMediaPreview from "@/components/SearchHitMediaPreview.vue";
import { groupHitsByRecency } from "@/lib/searchHitGrouping";

const props = defineProps<{
  hits: SearchHit[];
  loadAttachment: ((attachmentId: string) => Promise<Blob>) | undefined;
  loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
}>();

const emit = defineEmits<{
  "open-media": [hit: SearchHit];
}>();

const groups = computed(() => groupHitsByRecency(props.hits));
</script>

<template>
  <div class="search-media-groups">
    <section
      v-for="group in groups"
      :key="group.key"
      class="search-media-group"
    >
      <h3>{{ group.title }}</h3>
      <div class="search-media-grid">
        <!-- 网格里只有画面，文件名和「[图片]」这类标签都不重复出现。 -->
        <SearchHitMediaPreview
          v-for="hit in group.hits"
          :key="hit.message_id"
          class="search-media-cell"
          :hit="hit"
          :load-attachment="loadAttachment"
          :load-thumbnail="loadThumbnail"
          @open-media="emit('open-media', hit)"
        />
      </div>
    </section>
  </div>
</template>

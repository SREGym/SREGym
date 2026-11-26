package airport.meal;

import static airport.meal.MealBridge.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.IOException;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;

import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.TopicDescription;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.consumer.OffsetAndMetadata;
import org.apache.kafka.common.TopicPartition;

final class Issuer {
    static void run() throws Exception {
        String mode = System.getenv().getOrDefault("ISSUANCE_MODE", "legacy");
        if (!List.of("legacy", "revision").contains(mode)) {
            throw new IllegalArgumentException("ISSUANCE_MODE must be legacy or revision");
        }
        String inputId;
        String outputId;
        try (Admin admin = Admin.create(brokerSettings())) {
            Map<String, TopicDescription> topics = admin.describeTopics(List.of(INPUT, OUTPUT)).allTopicNames().get();
            inputId = topics.get(INPUT).topicId().toString();
            outputId = topics.get(OUTPUT).topicId().toString();
        }
        Properties settings = consumerSettings();
        settings.setProperty("group.id", GROUP);
        settings.setProperty("auto.offset.reset", "earliest");
        settings.setProperty("max.poll.records", "1");
        try (KafkaConsumer<String, String> consumer = new KafkaConsumer<>(settings)) {
            consumer.subscribe(List.of(OUTPUT));
            Map<TopicPartition, OffsetAndMetadata> committed = Map.of();
            while (!STOPPING.get()) {
                var records = consumer.poll(POLL);
                if (!consumer.assignment().isEmpty()) {
                    ready();
                }
                boolean delivered = true;
                for (ConsumerRecord<String, String> record : records) {
                    try {
                        deliver(record, mode, inputId, outputId);
                    } catch (IOException failure) {
                        System.err.printf("Issue %s-%d@%d retrying: %s%n",
                                record.topic(), record.partition(), record.offset(), failure.getMessage());
                        consumer.seek(new TopicPartition(record.topic(), record.partition()), record.offset());
                        delivered = false;
                        Thread.sleep(1000);
                    }
                }
                if (delivered) {
                    // Poll positions also cover transaction control offsets and aborted records.
                    Map<TopicPartition, OffsetAndMetadata> next = new HashMap<>();
                    for (TopicPartition partition : consumer.assignment()) {
                        next.put(partition, new OffsetAndMetadata(consumer.position(partition)));
                    }
                    if (!next.isEmpty() && !next.equals(committed)) {
                        consumer.commitSync(next);
                        committed = next;
                    }
                }
            }
        }
    }

    private static void deliver(ConsumerRecord<String, String> record, String mode, String inputId, String outputId)
            throws IOException, InterruptedException {
        JsonNode wire = JSON.readTree(record.value());
        ObjectNode order = (ObjectNode) wire.required("action").deepCopy();
        if (!INPUT.equals(text(wire, "input_topic"))) {
            throw new IOException("Unexpected input topic " + text(wire, "input_topic"));
        }
        if (text(order, "kind").equals("offer")) {
            // A shift-opening offer can carry a roster for the operations journal.
            order.remove("manifest");
        }
        order.set("input_partition", wire.required("input_partition"));
        order.set("input_offset", wire.required("input_offset"));
        JsonNode writer = wire.get("writer_job_id");
        if (writer != null) {
            if (!writer.isTextual() || writer.asText().isEmpty()) {
                throw new IOException("Unexpected writer_job_id in issue order");
            }
            order.set("writer_job_id", writer);
        }
        order.put("input_topic", INPUT).put("input_topic_id", inputId)
                .put("output_topic", record.topic()).put("output_topic_id", outputId)
                .put("output_partition", record.partition()).put("output_offset", record.offset());

        String deliveryPath = path("orders", outputId, Integer.toString(record.partition()),
                Long.toString(record.offset()));
        JsonNode receipt;
        try {
            receipt = post("clearing", "/orders/" + mode, order);
        } catch (PartnerError failure) {
            if (failure.status != 409 || writer == null) {
                throw failure;
            }
            // Earlier history can have a durable coordinate receipt from before
            // the issuer copied writer identity. Preserve that receipt on retry.
            JsonNode existing = get("clearing", deliveryPath);
            if (existing.has("writer_job_id")) {
                throw failure;
            }
            order.remove("writer_job_id");
            receipt = post("clearing", "/orders/" + mode, order);
        }
        String kind = text(order, "kind");
        if (mode.equals("revision")) {
            // A partially delivered legacy order may have its own printed number even
            // when the revision endpoint returns another alias for the same revision.
            receipt = get("clearing", deliveryPath);
            if (!kind.equals("offer")) {
                JsonNode current = get("clearing", path("benefits", text(order, "carrier"), text(order, "benefit_id")));
                for (JsonNode alias : get("clearing", "/aliases")) {
                    if (sameBenefit(alias, current)) {
                        catalog(current, text(alias, "voucher_id"));
                    }
                }
            }
        } else if (receipt.hasNonNull("voucher_id")) {
            catalog(order, text(receipt, "voucher_id"));
        }
        if (receipt.hasNonNull("voucher_id")) {
            ObjectNode handout = JSON.createObjectNode().put("action_id", text(order, "action_id"))
                    .put("voucher_id", text(receipt, "voucher_id"));
            post(text(order, "carrier"), "/handouts", handout);
        }
    }

    private static boolean sameBenefit(JsonNode one, JsonNode other) {
        return text(one, "carrier").equals(text(other, "carrier"))
                && text(one, "benefit_id").equals(text(other, "benefit_id"));
    }

    private static void catalog(JsonNode action, String voucher) throws IOException, InterruptedException {
        String cafe = text(action, "cafe");
        ObjectNode row = JSON.createObjectNode().put("voucher_id", voucher)
                .put("carrier", text(action, "carrier")).put("benefit_id", text(action, "benefit_id"))
                .put("active", !text(action, "kind").equals("withdraw"));
        row.set("revision", action.required("revision"));
        row.set("amount_cents", action.required("amount_cents"));
        try {
            post(cafe, "/catalog", row);
        } catch (PartnerError failure) {
            if (failure.status != 409) {
                throw failure;
            }
            JsonNode current = get(cafe, path("catalog", voucher));
            if (!sameBenefit(current, row) || (!row.equals(current)
                    && current.required("revision").asLong() <= row.required("revision").asLong())) {
                throw failure;
            }
        }
    }
}

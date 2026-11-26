package airport.meal;

import static airport.meal.MealBridge.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.OffsetSpec;
import org.apache.kafka.clients.admin.TopicDescription;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.clients.producer.RecordMetadata;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.utils.Utils;

final class Dispatch {
    private static final List<String> DESKS = List.of("north", "south");

    static void run() throws Exception {
        Properties settings = brokerSettings();
        settings.setProperty("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
        settings.setProperty("value.serializer", "org.apache.kafka.common.serialization.StringSerializer");
        settings.setProperty("acks", "all");
        settings.setProperty("buffer.memory", "4194304");
        settings.setProperty("max.block.ms", "20000");
        settings.setProperty("delivery.timeout.ms", "15000");
        settings.setProperty("transactional.id", "meal-dispatch");
        KafkaProducer<String, String> producer = new KafkaProducer<>(settings);
        try {
            // Settle any previous producer before checking which records actually committed.
            producer.initTransactions();
            TopicDescription topic;
            Map<String, Published> published;
            try (Admin admin = Admin.create(brokerSettings())) {
                topic = admin.describeTopics(List.of(INPUT)).allTopicNames().get().get(INPUT);
                published = recover(admin, topic);
            }
            if (STOPPING.get()) {
                return;
            }
            ready();
            while (!STOPPING.get()) {
                for (String desk : DESKS) {
                    try {
                        dispatch(desk, producer, topic, published);
                    } catch (IOException unavailable) {
                        System.err.println("Dispatch " + desk + " retrying: " + unavailable.getMessage());
                    }
                }
                Thread.sleep(1000);
            }
        } finally {
            producer.close(Duration.ofSeconds(3));
        }
    }

    private static Map<String, Published> recover(Admin admin, TopicDescription topic) throws Exception {
        Map<TopicPartition, OffsetSpec> latest = new HashMap<>();
        topic.partitions().forEach(partition -> latest.put(
                new TopicPartition(INPUT, partition.partition()), OffsetSpec.latest()));
        // A different open transaction can hide a committed send. Scan through the captured
        // high watermarks before using absence as evidence that it is safe to send again.
        var ends = admin.listOffsets(latest).all().get();
        Map<String, Published> records = new HashMap<>();
        Properties settings = consumerSettings();
        settings.setProperty("auto.offset.reset", "none");
        try (KafkaConsumer<String, String> consumer = new KafkaConsumer<>(settings)) {
            consumer.assign(ends.keySet());
            consumer.seekToBeginning(ends.keySet());
            Set<TopicPartition> remaining = new HashSet<>(ends.keySet());
            while (!remaining.isEmpty() && !STOPPING.get()) {
                for (ConsumerRecord<String, String> row : consumer.poll(POLL)) {
                    TopicPartition partition = new TopicPartition(row.topic(), row.partition());
                    if (row.offset() >= ends.get(partition).offset()) {
                        continue;
                    }
                    JsonNode action = JSON.readTree(row.value());
                    if (action.hasNonNull("carrier") && action.hasNonNull("action_id")) {
                        String key = actionKey(action);
                        Published previous = records.putIfAbsent(key,
                                new Published(action, row.partition(), row.offset()));
                        if (previous != null) {
                            previous.ambiguous = true;
                        }
                    }
                }
                Set<TopicPartition> scanned = remaining.stream()
                        .filter(partition -> consumer.position(partition) >= ends.get(partition).offset())
                        .collect(Collectors.toSet());
                consumer.pause(scanned);
                remaining.removeAll(scanned);
            }
        }
        return records;
    }

    private static void dispatch(String desk, KafkaProducer<String, String> producer,
            TopicDescription topic, Map<String, Published> published) throws Exception {
        if (STOPPING.get()) {
            return;
        }
        JsonNode state = get(desk, "/state");
        Set<String> acknowledged = new HashSet<>();
        for (JsonNode receipt : state.required("dispatches")) {
            String id = text(receipt, "action_id");
            acknowledged.add(id);
            published.remove(desk + '\0' + id);
        }
        Set<String> counterActions = new HashSet<>();
        for (JsonNode counter : state.required("counter_actions")) {
            counterActions.add(text(counter.required("action"), "action_id"));
        }
        for (JsonNode action : state.required("actions")) {
            if (STOPPING.get()) {
                return;
            }
            String id = text(action, "action_id");
            if (acknowledged.contains(id)) {
                continue;
            }
            String actionKey = actionKey(action);
            Published record = published.get(actionKey);
            if (record == null) {
                // Counter handling is recorded atomically with intake. An already
                // committed send still gets its genuine broker acknowledgment.
                if (counterActions.contains(id)) {
                    continue;
                }
                if (get(desk, "/dispatch-control").required("paused").asBoolean() || STOPPING.get()) {
                    return;
                }
                String benefit = desk + '\0' + text(action, "benefit_id");
                int partition = Utils.toPositive(Utils.murmur2(benefit.getBytes(StandardCharsets.UTF_8)))
                        % topic.partitions().size();
                producer.beginTransaction();
                RecordMetadata result = producer.send(
                        new ProducerRecord<>(INPUT, partition, benefit, JSON.writeValueAsString(action)))
                        .get(20, TimeUnit.SECONDS);
                // If send or commit is uncertain, exit and let producer initialization and
                // the retained-log scan on the next start resolve what actually committed.
                producer.commitTransaction();
                record = new Published(action, result.partition(), result.offset());
                published.put(actionKey, record);
            }
            if (record.ambiguous || !record.action.equals(action)) {
                throw new IOException("Kafka contains conflicting records for action " + id);
            }
            ObjectNode ack = JSON.createObjectNode().put("action_id", id).put("input_topic", INPUT)
                    .put("input_topic_id", topic.topicId().toString())
                    .put("input_partition", record.partition).put("input_offset", record.offset);
            post(desk, "/dispatches", ack);
            published.remove(actionKey);
            System.out.printf("Dispatched %s %s at %s-%d@%d%n", desk, id, INPUT, record.partition, record.offset);
        }
    }

    private static String actionKey(JsonNode action) {
        return text(action, "carrier") + '\0' + text(action, "action_id");
    }

    private static final class Published {
        final JsonNode action;
        final int partition;
        final long offset;
        boolean ambiguous;

        Published(JsonNode action, int partition, long offset) {
            this.action = action;
            this.partition = partition;
            this.offset = offset;
        }
    }
}

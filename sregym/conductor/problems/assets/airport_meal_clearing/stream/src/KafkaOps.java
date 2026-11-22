package airport.meal;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.TreeMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.AlterConfigOp;
import org.apache.kafka.clients.admin.ConfigEntry;
import org.apache.kafka.clients.admin.NewTopic;
import org.apache.kafka.clients.admin.TopicDescription;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.consumer.OffsetAndMetadata;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.clients.producer.RecordMetadata;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.config.ConfigResource;
import org.apache.kafka.common.errors.TopicExistsException;
import org.apache.kafka.common.errors.UnknownTopicOrPartitionException;

public final class KafkaOps {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String STRING_SERIALIZER =
            "org.apache.kafka.common.serialization.StringSerializer";
    private static final String STRING_DESERIALIZER =
            "org.apache.kafka.common.serialization.StringDeserializer";

    public static void main(String[] args) throws Exception {
        Properties settings = new Properties();
        settings.setProperty("bootstrap.servers", args[0]);
        settings.setProperty("request.timeout.ms", "10000");
        settings.setProperty("default.api.timeout.ms", "30000");
        try (BufferedReader input = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            JsonNode request = JSON.readTree(input.readLine());
            String op = request.required("op").asText();
            String topic = request.path("topic").asText("allowances");
            if (op.equals("produce") || op.equals("transaction")) {
                produce(settings, topic, op, request, input);
            } else {
                try (Admin admin = Admin.create(settings)) {
                    emit(adminOperation(admin, settings, topic, op, request));
                }
            }
        }
    }

    private static void emit(Object value) throws Exception {
        System.out.println(JSON.writeValueAsString(value));
    }

    private static void produce(Properties settings, String topic, String op,
            JsonNode request, BufferedReader input) throws Exception {
        settings.setProperty("key.serializer", STRING_SERIALIZER);
        settings.setProperty("value.serializer", STRING_SERIALIZER);
        settings.setProperty("acks", "all");
        settings.setProperty("buffer.memory", "4194304");
        settings.setProperty("delivery.timeout.ms", "30000");
        settings.setProperty("max.block.ms", "30000");
        boolean transactional = op.equals("transaction");
        if (transactional) {
            settings.setProperty("transactional.id", request.required("transactional_id").asText());
            settings.setProperty("transaction.timeout.ms", "600000");
        }
        try (KafkaProducer<String, String> producer = new KafkaProducer<>(settings)) {
            if (transactional) {
                producer.initTransactions();
                producer.beginTransaction();
            }
            List<Map<String, Object>> receipts = new ArrayList<>();
            try {
                List<Future<RecordMetadata>> sent = new ArrayList<>();
                for (JsonNode record : request.required("records")) {
                    String value = record.required(1).isTextual()
                            ? record.required(1).asText() : JSON.writeValueAsString(record.required(1));
                    sent.add(producer.send(new ProducerRecord<>(topic,
                            record.required(0).asInt(), null, value)));
                }
                for (Future<RecordMetadata> future : sent) {
                    RecordMetadata row = future.get(35, TimeUnit.SECONDS);
                    receipts.add(Map.of("topic", row.topic(), "partition", row.partition(),
                            "offset", row.offset()));
                }
                emit(receipts);
            } catch (Exception failure) {
                if (transactional) {
                    try {
                        producer.abortTransaction();
                    } catch (Exception abortFailure) {
                        failure.addSuppressed(abortFailure);
                    }
                }
                throw failure;
            }
            if (transactional) {
                boolean commit = "commit".equals(input.readLine());
                if (commit) {
                    producer.commitTransaction();
                } else {
                    producer.abortTransaction();
                }
                emit(Map.of("status", commit ? "committed" : "aborted", "receipts", receipts));
            }
        }
    }

    private static Object adminOperation(Admin admin, Properties settings, String topic,
            String op, JsonNode request) throws Exception {
        ConfigResource resource = new ConfigResource(ConfigResource.Type.TOPIC, topic);
        switch (op) {
            case "ensure_topic": {
                int count = request.required("partitions").asInt();
                NewTopic definition = new NewTopic(topic, count, (short) 1);
                Map<String, String> configs = new HashMap<>();
                request.path("settings").fields().forEachRemaining(
                        entry -> configs.put(entry.getKey(), entry.getValue().asText()));
                try {
                    admin.createTopics(List.of(definition.configs(configs))).all().get();
                } catch (ExecutionException failure) {
                    if (!(failure.getCause() instanceof TopicExistsException)) {
                        throw failure;
                    }
                }
                if (awaitTopic(admin, topic).partitions().size() != count) {
                    throw new IllegalStateException("Unexpected partition count for " + topic);
                }
                return Map.of();
            }
            case "topic_id":
                return describe(admin, topic).topicId().toString();
            case "topic_config": {
                Map<String, String> config = new TreeMap<>();
                for (ConfigEntry entry : admin.describeConfigs(List.of(resource)).all().get()
                        .get(resource).entries()) {
                    config.put(entry.name(), entry.value());
                }
                return config;
            }
            case "alter_topic_config": {
                List<AlterConfigOp> changes = new ArrayList<>();
                request.required("settings").fields().forEachRemaining(entry -> {
                    boolean remove = entry.getValue().isNull();
                    changes.add(new AlterConfigOp(
                            new ConfigEntry(entry.getKey(), remove ? null : entry.getValue().asText()),
                            remove ? AlterConfigOp.OpType.DELETE : AlterConfigOp.OpType.SET));
                });
                if (!changes.isEmpty()) {
                    admin.incrementalAlterConfigs(Map.of(resource, changes)).all().get();
                }
                return Map.of();
            }
            case "group_offsets": {
                Map<Integer, Long> result = new TreeMap<>();
                admin.listConsumerGroupOffsets(request.required("group").asText())
                        .partitionsToOffsetAndMetadata().get().forEach((tp, offset) -> {
                            if (tp.topic().equals(topic) && offset != null) {
                                result.put(tp.partition(), offset.offset());
                            }
                        });
                return result;
            }
            case "set_group_offsets": {
                Map<TopicPartition, OffsetAndMetadata> changes = new HashMap<>();
                request.required("offsets").fields().forEachRemaining(entry -> changes.put(
                        new TopicPartition(topic, Integer.parseInt(entry.getKey())),
                        new OffsetAndMetadata(entry.getValue().asLong())));
                if (!changes.isEmpty()) {
                    admin.alterConsumerGroupOffsets(request.required("group").asText(), changes)
                            .all().get();
                }
                return Map.of();
            }
            case "bounds":
            case "read":
                return consume(settings, topic, describe(admin, topic), op, request);
            default:
                throw new IllegalArgumentException("Unknown operation " + op);
        }
    }

    private static TopicDescription describe(Admin admin, String topic) throws Exception {
        return admin.describeTopics(List.of(topic)).allTopicNames().get().get(topic);
    }

    private static TopicDescription awaitTopic(Admin admin, String topic) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(25);
        while (true) {
            try {
                return describe(admin, topic);
            } catch (ExecutionException failure) {
                if (!(failure.getCause() instanceof UnknownTopicOrPartitionException)
                        || System.nanoTime() >= deadline) {
                    throw failure;
                }
                Thread.sleep(250);
            }
        }
    }

    private static Object consume(Properties settings, String topic, TopicDescription description,
            String op, JsonNode request) {
        settings.setProperty("key.deserializer", STRING_DESERIALIZER);
        settings.setProperty("value.deserializer", STRING_DESERIALIZER);
        settings.setProperty("enable.auto.commit", "false");
        settings.setProperty("auto.offset.reset", "none");
        settings.setProperty("isolation.level", request.required("isolation").asText());
        List<TopicPartition> partitions = description.partitions().stream()
                .map(info -> new TopicPartition(topic, info.partition())).collect(Collectors.toList());
        try (KafkaConsumer<String, String> consumer = new KafkaConsumer<>(settings)) {
            consumer.assign(partitions);
            Map<TopicPartition, Long> starts = consumer.beginningOffsets(partitions);
            Map<TopicPartition, Long> ends = consumer.endOffsets(partitions);
            if (op.equals("bounds")) {
                Map<Integer, Map<String, Long>> bounds = new TreeMap<>();
                for (TopicPartition tp : partitions) {
                    bounds.put(tp.partition(), Map.of("start", starts.get(tp), "end", ends.get(tp)));
                }
                return bounds;
            }
            List<TopicPartition> pending = new ArrayList<>();
            for (TopicPartition tp : partitions) {
                long start = request.path("offsets").path(Integer.toString(tp.partition()))
                        .asLong(starts.get(tp));
                if (start < starts.get(tp)) {
                    throw new IllegalArgumentException("Offset " + start + " precedes the start of " + tp);
                }
                consumer.seek(tp, start);
                if (start < ends.get(tp)) {
                    pending.add(tp);
                } else {
                    consumer.pause(List.of(tp));
                }
            }
            List<Map<String, Object>> result = new ArrayList<>();
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
            while (!pending.isEmpty()) {
                if (System.nanoTime() >= deadline) {
                    throw new IllegalStateException("Timed out reading partitions " + pending);
                }
                consumer.poll(Duration.ofMillis(300)).forEach(record -> {
                    TopicPartition tp = new TopicPartition(topic, record.partition());
                    if (record.offset() < ends.get(tp)) {
                        Map<String, Object> item = new HashMap<>();
                        item.put("topic", topic);
                        item.put("partition", record.partition());
                        item.put("offset", record.offset());
                        item.put("value", record.value());
                        result.add(item);
                    }
                });
                List<TopicPartition> complete = pending.stream()
                        .filter(tp -> consumer.position(tp) >= ends.get(tp))
                        .collect(Collectors.toList());
                consumer.pause(complete);
                pending.removeAll(complete);
            }
            result.sort(Comparator.comparingInt((Map<String, Object> row) -> (int) row.get("partition"))
                    .thenComparingLong(row -> (long) row.get("offset")));
            return result;
        }
    }
}

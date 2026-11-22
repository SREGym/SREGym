package airport.meal;

import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.functions.RichMapFunction;
import org.apache.flink.api.common.serialization.SimpleStringSchema;
import org.apache.flink.api.common.typeinfo.Types;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.configuration.ExecutionOptions;
import org.apache.flink.connector.base.DeliveryGuarantee;
import org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaSink;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.connector.kafka.source.reader.deserializer.KafkaRecordDeserializationSchema;
import org.apache.flink.kafka.shaded.org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.flink.kafka.shaded.org.apache.kafka.clients.consumer.OffsetResetStrategy;
import org.apache.flink.kafka.shaded.org.apache.kafka.common.TopicPartition;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.util.Collector;

public final class MealOrders {
    public static void main(String[] args) throws Exception {
        String broker = args[0];
        String group = args[1];
        String output = args[2];
        String name = args[3];
        String readerName = args[4];
        boolean compression = Boolean.parseBoolean(args[5]);
        int parallelism = Integer.parseInt(args[6]);
        long checkpointMs = Long.parseLong(args[7]);

        Configuration config = new Configuration();
        config.set(ExecutionOptions.SNAPSHOT_COMPRESSION, compression);
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment(config);
        env.setParallelism(parallelism);
        env.enableCheckpointing(checkpointMs);

        KafkaSource<String> source = KafkaSource.<String>builder()
                .setBootstrapServers(broker)
                .setTopics("allowances")
                .setGroupId(group)
                .setStartingOffsets(initializer(args[8], args[9]))
                .setProperty("isolation.level", "read_committed")
                .setProperty("partition.discovery.interval.ms", "5000")
                .setProperty("commit.offsets.on.checkpoint", "true")
                .setDeserializer(new OrderLine())
                .build();

        KafkaSink<String> sink = KafkaSink.<String>builder()
                .setBootstrapServers(broker)
                .setProperty("buffer.memory", "4194304")
                .setRecordSerializer(KafkaRecordSerializationSchema.builder()
                        .setTopic(output)
                        .setValueSerializationSchema(new SimpleStringSchema())
                        .build())
                .setDeliveryGuarantee(DeliveryGuarantee.AT_LEAST_ONCE)
                .build();

        env.fromSource(source, WatermarkStrategy.noWatermarks(), readerName)
                .uid("meal-allowances")
                .map(new WriterIdentity())
                .name("Record publishing job")
                .uid("meal-writer-identity")
                .sinkTo(sink)
                .name("Publish " + output)
                .uid("meal-orders");
        env.execute(name);
    }

    public static final class WriterIdentity extends RichMapFunction<String, String> {
        private transient String prefix;

        @Override
        public void open(Configuration parameters) {
            prefix = "{\"writer_job_id\":\"" + getRuntimeContext().getJobId().toHexString() + "\",";
        }

        @Override
        public String map(String envelope) {
            return prefix + envelope.substring(1);
        }
    }

    private static OffsetsInitializer initializer(String start, String argument) {
        switch (start) {
            case "earliest":
                return OffsetsInitializer.earliest();
            case "latest":
                return OffsetsInitializer.latest();
            case "committed":
                return OffsetsInitializer.committedOffsets(OffsetResetStrategy.valueOf(argument));
            case "explicit":
                Map<TopicPartition, Long> offsets = new HashMap<>();
                for (String entry : argument.split(",")) {
                    String[] fields = entry.split(":", 2);
                    offsets.put(new TopicPartition("allowances", Integer.parseInt(fields[0])),
                            Long.parseLong(fields[1]));
                }
                return OffsetsInitializer.offsets(offsets, OffsetResetStrategy.NONE);
            default:
                throw new IllegalArgumentException("Unknown starting position " + start);
        }
    }

    public static final class OrderLine implements KafkaRecordDeserializationSchema<String> {
        @Override
        public void deserialize(ConsumerRecord<byte[], byte[]> record, Collector<String> out) {
            String action = new String(record.value(), StandardCharsets.UTF_8);
            out.collect("{\"input_topic\":\"" + record.topic() + "\",\"input_partition\":"
                    + record.partition() + ",\"input_offset\":" + record.offset() + ",\"action\":" + action + "}");
        }

        @Override
        public org.apache.flink.api.common.typeinfo.TypeInformation<String> getProducedType() {
            return Types.STRING;
        }
    }
}

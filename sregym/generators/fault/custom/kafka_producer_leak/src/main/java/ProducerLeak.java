import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.common.serialization.StringSerializer;
import org.apache.kafka.common.serialization.ByteArraySerializer;
import java.util.Properties;
import java.util.Random;

public class ProducerLeak {
    public static void main(String[] args) throws InterruptedException {
        int payloadSizeBytes = Integer.parseInt(System.getenv().getOrDefault("PAYLOAD_SIZE_BYTES", "10000000")); // 10MB

        Properties props = new Properties();
        props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, "kafka:9092");
        props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
        props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, ByteArraySerializer.class.getName());
        props.put(ProducerConfig.MAX_REQUEST_SIZE_CONFIG, payloadSizeBytes + 1000);
        props.put(ProducerConfig.BUFFER_MEMORY_CONFIG, (long) payloadSizeBytes * 2);

        KafkaProducer<String, byte[]> producer = new KafkaProducer<>(props);

        byte[] payload = new byte[payloadSizeBytes];
        new Random().nextBytes(payload);

        int count = 0;
        while (true) {
            producer.send(new ProducerRecord<>("orders", payload));
            count++;
            System.out.println("Sent payload #" + count);
        }
    }
}